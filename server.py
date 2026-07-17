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
import main as core
from dotenv import load_dotenv
load_dotenv()


app = FastAPI()

# Shared secret configured on GitHub's webhook settings page. Used to verify
# that incoming /webhook requests genuinely came from GitHub, via HMAC-SHA256
# over the raw request body (GitHub sends this as the X-Hub-Signature-256
# header). Without this check, anyone who finds the endpoint URL could POST
# fake payloads and trigger scans/comments as if they were real GitHub events.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Personal access token used to post comments back onto a PR via the
# GitHub REST API. Needs "Pull requests: Read and write" permission,
# scoped to the specific repo(s) CodeScope is installed on.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Only https://github.com/<owner>/<repo>(.git)? is accepted.
# Owner/repo segments follow GitHub's allowed charset: alphanumeric, hyphen,
# underscore, and dot (GitHub itself disallows a leading/trailing hyphen, but
# we don't need to be that strict here -- we just need to reject anything
# that isn't plausibly a GitHub repo path).
GITHUB_REPO_PATTERN = re.compile(r"^/[\w.\-]+/[\w.\-]+(\.git)?/?$")


def validate_repo_url(repo_url: str) -> None:
    """Raises ValueError with a human-readable reason if repo_url is not a
    safe, well-formed https://github.com/<owner>/<repo> URL.

    This guards against two real risks:
      1. SSRF / arbitrary-host access -- someone passing a file:// URL, an
         internal network address, or a non-GitHub host.
      2. Git argument injection -- a string starting with "-" can be
         interpreted by `git clone` as a flag (e.g. --upload-pack=...)
         rather than a URL, a known real vulnerability class.
    """
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
    """Returns True only if signature_header is a valid HMAC-SHA256 signature
    of raw_body, computed with WEBHOOK_SECRET -- proving the request really
    came from GitHub and wasn't forged or tampered with in transit.

    Must be checked against the exact raw bytes GitHub sent, not a re-parsed/
    re-serialized JSON body -- re-serializing can change whitespace/key order
    and would make a genuine signature fail to match.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not WEBHOOK_SECRET:
        # Misconfiguration on our end -- fail closed, not open.
        return False

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")

    # Constant-time comparison -- a naive `==` leaks timing information that
    # can be used to guess the correct signature byte-by-byte.
    return hmac.compare_digest(expected, provided)


class ScanRequest(BaseModel):
    repo_url: str


def get_pr_changed_files(repo_full_name: str, pr_number: int) -> list[str]:
    """Returns the basenames of files actually changed in this PR, by calling
    GitHub's REST API. This is what lets the analysis scope itself to what a
    PR actually touched, instead of reporting on the whole repo every time.

    Note: matches on basename only (not full path), consistent with how
    parser.py currently stores just the filename on each fact -- a known
    simplification that would only misbehave if two files with the same
    name exist in different folders of the same repo.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files"
    try:
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        response.raise_for_status()
        files = response.json()
        return [os.path.basename(f["filename"]) for f in files]
    except requests.exceptions.RequestException:
        return []


def post_pr_comment(repo_full_name: str, pr_number: int, body: str):
    """Posts a comment onto the given PR using the GitHub REST API."""
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    try:
        requests.post(
            url,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"body": body},
            timeout=15,
        )
    except requests.exceptions.RequestException:
        # Posting the comment failed (bad token, network issue, etc).
        # Nothing further we can do here -- this already runs in a
        # background task with no one waiting on its return value.
        pass


def process_pr_event(repo_full_name: str, clone_url: str, pr_number: int):
    """Runs in the background after a verified pull_request webhook event:
    clones the PR's repo, builds the dependency graph, figures out which
    functions were actually defined in files this PR changed, and posts a
    risk report scoped to just those -- not a generic whole-repo summary."""
    try:
        validate_repo_url(clone_url.removesuffix(".git"))
    except ValueError as e:
        post_pr_comment(repo_full_name, pr_number, f"CodeScope could not process this repo: {e}")
        return

    changed_files = get_pr_changed_files(repo_full_name, pr_number)

    temp_dir = tempfile.mkdtemp()
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, temp_dir],
            check=True, capture_output=True, timeout=60
        )
        facts = extract_facts_from_folder(temp_dir)

        graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD)
        graph.clear_all()
        graph.insert_facts(facts)

        # Functions actually defined in files this PR touched -- this is
        # what scopes the report to the PR instead of the whole repo.
        changed_function_names = sorted({
            f["name"] for f in facts
            if f["type"] == "FUNCTION_DEFINED" and f["file"] in changed_files
        })

        if not changed_function_names:
            graph.close()
            post_pr_comment(
                repo_full_name, pr_number,
                "## CodeScope Analysis\n\n"
                "No analyzable functions were found in the files this PR changed "
                "(e.g. docs-only changes, or a language CodeScope doesn't parse yet). "
                "No risk report to show for this one."
            )
            return

        comment_body = "## CodeScope Analysis\n\n"
        comment_body += f"This PR changes **{len(changed_function_names)} function(s)**. Risk report:\n\n"

        for fn_name in changed_function_names:
            affected = graph.find_impact(fn_name)
            report = score_risk(affected)
            comment_body += f"### `{fn_name}` \u2014 Risk: **{report['risk_level']}**\n"
            if report["affected_count"] == 0:
                comment_body += "Nothing else in this codebase currently depends on this function.\n\n"
            else:
                comment_body += f"Affects **{report['affected_count']}** other function(s):\n"
                for aff in report["affected_functions"][:10]:
                    comment_body += f"- `{aff['name']}` (in `{aff['file']}`)\n"
                if report["affected_count"] > 10:
                    comment_body += f"- ...and {report['affected_count'] - 10} more\n"
                comment_body += "\n"

        graph.close()
        post_pr_comment(repo_full_name, pr_number, comment_body)

    except subprocess.CalledProcessError as e:
        post_pr_comment(repo_full_name, pr_number, f"CodeScope could not clone this repo: {e.stderr.decode(errors='ignore')}")
    except subprocess.TimeoutExpired:
        post_pr_comment(repo_full_name, pr_number, "CodeScope timed out while cloning this repo.")
    except Exception as e:
        post_pr_comment(repo_full_name, pr_number, f"CodeScope analysis failed: {e}")
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
        background_tasks.add_task(process_pr_event, repo_full_name, clone_url, pr_number)
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
        subprocess.run(
            ["git", "clone", "--depth", "1", req.repo_url, temp_dir],
            check=True, capture_output=True, timeout=60
        )
        facts = extract_facts_from_folder(temp_dir)

        graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD)
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
    graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD)
    central = graph.find_central_functions(5)
    graph.close()
    return {"central_functions": central}


@app.get("/impact/{function_name}")
def impact(function_name: str):
    graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD)
    affected = graph.find_impact(function_name)
    graph.close()
    return score_risk(affected)


@app.get("/explain")
def explain():
    graph = CodeGraph(core.NEO4J_URI, core.NEO4J_USERNAME, core.NEO4J_PASSWORD)
    central = graph.find_central_functions(5)
    edges = graph.get_all_edges()
    graph.close()
    explanation = explain_codebase(central, edges)
    return {"explanation": explanation}