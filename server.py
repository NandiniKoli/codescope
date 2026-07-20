import subprocess
import tempfile
import shutil
import re
import os
import hmac
import hashlib
import requests
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
from parser import extract_facts_from_folder
from graph import CodeGraph
from risk import score_risk
from explain import explain_codebase
from github_app import get_installation_token
import main as core


app = FastAPI()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Scan ID used for manual/API testing via /scan + /map + /impact + /explain
# (e.g. through the Swagger UI), so a manual test sequence reads back its
# own data consistently, isolated from any real webhook-triggered PR scans.
MANUAL_SCAN_ID = "api-manual"

GITHUB_REPO_PATTERN = re.compile(r"^/[\w.\-]+/[\w.\-]+(\.git)?/?$")


def validate_repo_url(repo_url: str) -> None:
    """Raises ValueError if repo_url is not a safe, well-formed
    https://github.com/<owner>/<repo> URL. Guards against SSRF/arbitrary-host
    access and git argument injection (a leading "-" being read as a flag)."""
    if not repo_url or repo_url.startswith("-"):
        raise ValueError("repo_url is empty or looks like a command flag.")

    parsed = urlparse(repo_url)

    if parsed.scheme != "https":
        raise ValueError("repo_url must use https://.")
    if parsed.hostname not in ("github.com", "www.github.com"):
        raise ValueError("repo_url must point to github.com.")
    if parsed.username or parsed.password or (parsed.port not in (None, 443)):
        raise ValueError("repo_url must not contain credentials or a custom port.")
    if not GITHUB_REPO_PATTERN.match(parsed.path):
        raise ValueError("repo_url must look like https://github.com/<owner>/<repo>.")


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifies the request genuinely came from GitHub via HMAC-SHA256,
    using a constant-time comparison to avoid leaking timing information."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not WEBHOOK_SECRET:
        return False

    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def clone_repo(clone_url: str, temp_dir: str):
    """Clones with a retry, since a rare Windows/subprocess buffering issue
    ('Existing exports of data: object cannot be re-sized') can intermittently
    fail the first attempt on some repos."""
    last_error = None
    for attempt in range(2):
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, temp_dir],
                check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL
            )
            return
        except OSError as e:
            last_error = e
    raise last_error


class ScanRequest(BaseModel):
    repo_url: str


def post_pr_comment(repo_full_name: str, pr_number: int, body: str, installation_token: str):
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    try:
        requests.post(
            url,
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": body},
            timeout=15,
        )
    except requests.exceptions.RequestException:
        pass


def process_pr_event(repo_full_name: str, clone_url: str, pr_number: int, installation_id: int):
    """Runs in the background after a verified pull_request webhook event.
    Uses a scan_id unique to this repo+PR so concurrent events (e.g. the
    same repo getting multiple quick PR updates) never wipe or read each
    other's graph data."""
    try:
        installation_token = get_installation_token(installation_id)
    except Exception as e:
        print(f"Failed to get installation token: {e}")
        return

    scan_id = f"{repo_full_name}#{pr_number}"

    try:
        validate_repo_url(clone_url.removesuffix(".git"))
    except ValueError as e:
        post_pr_comment(repo_full_name, pr_number, f"CodeScope could not process this repo: {e}", installation_token)
        return

    temp_dir = tempfile.mkdtemp()
    try:
        clone_repo(clone_url, temp_dir)
        facts = extract_facts_from_folder(temp_dir)

        graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD, scan_id)
        graph.clear_all()
        graph.insert_facts(facts)
        central = graph.find_central_functions(5)
        edges = graph.get_all_edges()
        graph.close()

        explanation = explain_codebase(central, edges)

        comment_body = "## CodeScope Analysis\n\n" + explanation + "\n\n"
        comment_body += "**Most important functions in this codebase:**\n"
        for fn in central:
            comment_body += f"- `{fn['name']}` (in `{fn['file']}`) \u2014 called from {fn['incoming_calls']} place(s)\n"

        post_pr_comment(repo_full_name, pr_number, comment_body, installation_token)

    except subprocess.CalledProcessError as e:
        post_pr_comment(repo_full_name, pr_number, f"CodeScope could not clone this repo: {e.stderr.decode(errors='ignore')}", installation_token)
    except subprocess.TimeoutExpired:
        post_pr_comment(repo_full_name, pr_number, "CodeScope timed out while cloning this repo.", installation_token)
    except Exception as e:
        post_pr_comment(repo_full_name, pr_number, f"CodeScope analysis failed: {e}", installation_token)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook signature.")

    event_type = request.headers.get("X-GitHub-Event", "unknown")
    payload = await request.json()

    if event_type == "ping":
        return {"status": "pong"}

    if event_type == "pull_request" and payload.get("action") in ("opened", "synchronize"):
        repo_full_name = payload["repository"]["full_name"]
        clone_url = payload["repository"]["clone_url"]
        pr_number = payload["pull_request"]["number"]
        installation_id = payload["installation"]["id"]
        background_tasks.add_task(process_pr_event, repo_full_name, clone_url, pr_number, installation_id)
        return {"status": "processing"}

    return {"status": "ignored", "event": event_type}


@app.post("/scan")
def scan(req: ScanRequest):
    try:
        validate_repo_url(req.repo_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    temp_dir = tempfile.mkdtemp()
    try:
        clone_repo(req.repo_url, temp_dir)
        facts = extract_facts_from_folder(temp_dir)

        graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD, MANUAL_SCAN_ID)
        graph.clear_all()
        graph.insert_facts(facts)
        graph.close()

        return {"facts_extracted": len(facts), "repo": req.repo_url}

    except subprocess.CalledProcessError as e:
        return {"error": f"Could not clone repo: {e.stderr.decode()}"}
    except subprocess.TimeoutExpired:
        return {"error": "Cloning the repo took too long (timed out)."}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/map")
def code_map():
    graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD, MANUAL_SCAN_ID)
    central = graph.find_central_functions(5)
    graph.close()
    return {"central_functions": central}


@app.get("/impact/{function_name}")
def impact(function_name: str):
    graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD, MANUAL_SCAN_ID)
    affected = graph.find_impact(function_name)
    graph.close()
    return score_risk(affected)


@app.get("/explain")
def explain():
    graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD, MANUAL_SCAN_ID)
    central = graph.find_central_functions(5)
    edges = graph.get_all_edges()
    graph.close()
    explanation = explain_codebase(central, edges)
    return {"explanation": explanation}