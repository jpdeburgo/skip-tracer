"""Seen-parcel state persistence.

Same pattern as job-finder/nw-deal-screener: a local JSON file committed to
the repo by default, or a GitHub-backed file (read/written via the Contents
API) when ST_GITHUB_TOKEN + ST_GITHUB_REPO are set — needed on Render, whose
cron jobs get a fresh, ephemeral disk each run. Either way, re-runs skip
already-processed parcels and don't re-pay BatchData for the same
skip-trace.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

STATE_FILE = Path(__file__).resolve().parent.parent.parent / "state.json"
REQUEST_TIMEOUT_SECONDS = 20


def _github_state_config(env: dict[str, str]) -> tuple[str, str, str] | None:
    token = env.get("ST_GITHUB_TOKEN")
    repo = env.get("ST_GITHUB_REPO")
    if bool(token) != bool(repo):
        raise RuntimeError("ST_GITHUB_TOKEN and ST_GITHUB_REPO must be set together")
    if not token or not repo:
        return None
    return token, repo, env.get("ST_GIT_BRANCH", "main")


def _github_state_request(
    method: str, token: str, repo: str, **kwargs: Any
) -> requests.Response:
    url = f"https://api.github.com/repos/{repo}/contents/{STATE_FILE.name}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return requests.request(
        method, url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs
    )


def load_seen_parcels(env: dict[str, str] | None = None) -> set[str]:
    env = env if env is not None else dict(os.environ)
    config = _github_state_config(env)
    if config is not None:
        token, repo, branch = config
        response = _github_state_request(
            "GET", token, repo, params={"ref": branch}
        )
        if response.status_code == 404:
            return set()
        response.raise_for_status()
        content = base64.b64decode(response.json()["content"]).decode("utf-8")
    elif STATE_FILE.exists():
        content = STATE_FILE.read_text(encoding="utf-8")
    else:
        return set()

    data = json.loads(content)
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{STATE_FILE.name} must contain a JSON array of ACCTIDs")
    return set(data)


def save_seen_parcels(seen: set[str], env: dict[str, str] | None = None) -> None:
    env = env if env is not None else dict(os.environ)
    content = json.dumps(sorted(seen), indent=2) + "\n"
    config = _github_state_config(env)
    if config is not None:
        token, repo, branch = config
        current = _github_state_request(
            "GET", token, repo, params={"ref": branch}
        )
        if current.status_code not in (200, 404):
            current.raise_for_status()
        payload: dict[str, Any] = {
            "message": f"Update seen parcels - {datetime.now():%Y-%m-%d %H:%M}",
            "content": base64.b64encode(content.encode()).decode("ascii"),
            "branch": branch,
        }
        if current.status_code == 200:
            payload["sha"] = current.json()["sha"]
        response = _github_state_request("PUT", token, repo, json=payload)
        response.raise_for_status()
        print(f"Updated {STATE_FILE.name} on GitHub.")
        return

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=STATE_FILE.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(STATE_FILE)
