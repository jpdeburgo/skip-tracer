"""Minimal Gmail API authentication and sending helpers.

Matches the OAuth2 pattern used in the job-finder and nw-deal-screener
projects — reuse those credentials/refresh token if still valid rather than
re-authorizing from scratch (see README.md's Gmail API setup section).
"""

from __future__ import annotations

import base64
import errno
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _save_token(token_path: Path, credentials: Credentials) -> None:
    """Cache refreshed credentials unless the configured secret is read-only."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
        print(
            f"Gmail token refreshed in memory; skipping write to read-only "
            f"{token_path}."
        )


def get_gmail_service() -> Any:
    """Authorize Gmail, refreshing or bootstrapping a cached OAuth token."""
    credentials_path = Path(
        os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
    )
    token_path = Path(os.environ.get("GMAIL_TOKEN_PATH", "token.json"))

    credentials: Credentials | None = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not credentials.has_scopes(SCOPES):
            credentials = None

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth client secret not found at {credentials_path}. "
                    "See README.md for Gmail API setup."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            credentials = flow.run_local_server(port=0)

        _save_token(token_path, credentials)

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def send_email(
    service: Any,
    subject: str,
    body_text: str,
    recipient: str,
    html: bool = False,
) -> str:
    """Send a plain-text or HTML email and return the Gmail message ID."""
    message = MIMEText(body_text, "html" if html else "plain", "utf-8")
    message["To"] = recipient
    message["Subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["id"]
