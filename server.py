import subprocess
import tempfile
import shutil
import re
import os
import hmac
import hashlib
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from parser import extract_facts_from_folder
from graph import CodeGraph
from risk import score_risk
from explain import explain_codebase
import main as core

app = FastAPI()

ALLOWED_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def verify_github_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Verifies a GitHub webhook payload using HMAC-SHA256.

    GitHub sends the signature as 'sha256=<hex digest>' in the
    X-Hub-Signature-256 header, computed over the raw (unparsed) request
    body using the shared secret configured on the webhook. We must
    compute our own digest over the same raw bytes and compare using a
    constant-time comparison to avoid timing attacks.
    """
    if not WEBHOOK_SECRET:
        # Fail closed: if no secret is configured, refuse everything rather
        # than silently accepting unverified payloads.
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_digest = hmac.new(
        key=WEBHOOK_SECRET.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected_header = f"sha256={expected_digest}"

    return hmac.compare_digest(expected_header, signature_header)


@app.post("/webhook")
async def github_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_github_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid or missing signature")

    event_type = request.headers.get("X-GitHub-Event", "unknown")

    # Phase 3 stops here on purpose: signature is verified, but we don't yet
    # parse the payload or trigger a scan. That's Phase 4+.
    return {"status": "verified", "event": event_type}


class ScanRequest(BaseModel):
    repo_url: str


def validate_repo_url(url: str):
    """Basic guard against SSRF / unexpected clone targets on a public endpoint."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only https:// repo URLs are allowed.")
    if parsed.hostname not in ALLOWED_GIT_HOSTS:
        raise ValueError(f"Host '{parsed.hostname}' is not allowed.")
    if not re.match(r"^/[\w.-]+/[\w.-]+/?$", parsed.path):
        raise ValueError("URL must look like https://github.com/owner/repo")


@app.post("/scan")
def scan(req: ScanRequest):
    try:
        validate_repo_url(req.repo_url)
    except ValueError as e:
        return {"error": str(e)}

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