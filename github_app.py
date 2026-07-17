"""
github_app.py
Handles authenticating as the CodeScope GitHub App (not a personal token),
so it can post comments on any repo that has installed it.
"""

import os
import time
import jwt
import requests

GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_PRIVATE_KEY_PATH = os.environ.get("GITHUB_PRIVATE_KEY_PATH", "codescope-private-key.pem")


def generate_jwt():
    """Creates a short-lived JWT proving this request comes from the
    CodeScope GitHub App itself (not any specific installation yet)."""
    private_key = os.environ.get("GITHUB_PRIVATE_KEY", "")
    if not private_key:
        with open(GITHUB_PRIVATE_KEY_PATH, "r") as f:
            private_key = f.read()

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (9 * 60),
        "iss": GITHUB_APP_ID,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")

def get_installation_token(installation_id: int) -> str:
    """Exchanges the app-level JWT for a short-lived token scoped to one
    specific installation (i.e. one specific user/org that installed the app).
    This is the token actually used to post comments, clone private repos, etc."""
    app_jwt = generate_jwt()
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["token"]