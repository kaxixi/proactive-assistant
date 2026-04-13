"""Google OAuth2 authentication — shared by Gmail and Calendar modules."""

import os
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import ENABLE_EMAIL

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(PROJECT_DIR, "credentials.json")
TOKEN_FILE = os.path.join(PROJECT_DIR, "token.json")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
if ENABLE_EMAIL:
    SCOPES.insert(0, "https://www.googleapis.com/auth/gmail.readonly")


def token_age_days() -> float | None:
    """Return how many days since token.json was last regenerated, or None if missing.

    Used to warn before the 7-day refresh-token expiry on unverified OAuth apps.
    """
    if not os.path.exists(TOKEN_FILE):
        return None
    import time
    age_seconds = time.time() - os.path.getmtime(TOKEN_FILE)
    return age_seconds / 86400


def get_credentials() -> Credentials:
    """Return valid Google credentials, prompting OAuth flow if needed."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Token revoked or expired beyond refresh — need full re-auth
                os.remove(TOKEN_FILE)
                creds = None
        if not creds or not creds.valid:
            # On the VM, services run under systemd — no browser available.
            # Detect this and give a clear error instead of crashing.
            if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
                raise RuntimeError(
                    "Google token expired — re-generate locally and scp to VM:\n"
                    "  python3 -c \"from google_auth import get_credentials; get_credentials()\"\n"
                    f"  gcloud compute scp --zone=us-central1-a {TOKEN_FILE} claudette:~/proactive-assistant/"
                )
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_FILE} — download it from Google Cloud Console"
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds
