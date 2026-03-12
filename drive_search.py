"""Google Drive integration — search and retrieve files for meeting prep."""

import logging
from googleapiclient.discovery import build

from google_auth import get_credentials

logger = logging.getLogger(__name__)


def _get_drive_service():
    creds = get_credentials()
    return build("drive", "v3", credentials=creds)


def search_files(query: str, max_results: int = 5) -> list[dict]:
    """Search Drive for files matching a query.

    Returns list of dicts with: id, name, mimeType, modifiedTime, webViewLink
    """
    service = _get_drive_service()

    results = service.files().list(
        q=f"fullText contains '{query}' and trashed = false",
        pageSize=max_results,
        fields="files(id, name, mimeType, modifiedTime, webViewLink)",
        orderBy="modifiedTime desc",
    ).execute()

    files = results.get("files", [])
    logger.info(f"Drive search '{query}': found {len(files)} files")
    return files


def get_recent_files(max_results: int = 10) -> list[dict]:
    """Get recently modified files."""
    service = _get_drive_service()

    results = service.files().list(
        pageSize=max_results,
        fields="files(id, name, mimeType, modifiedTime, webViewLink)",
        orderBy="modifiedTime desc",
        q="trashed = false",
    ).execute()

    return results.get("files", [])


def find_files_for_meeting(meeting_summary: str) -> list[dict]:
    """Find Drive files that might be relevant to a meeting.

    Uses keywords from the meeting title to search.
    """
    # Extract meaningful words (skip short/common words)
    skip_words = {"the", "and", "for", "with", "meeting", "call", "chat",
                  "sync", "check", "weekly", "daily", "monthly", "update"}
    words = [w for w in meeting_summary.lower().split()
             if len(w) > 2 and w not in skip_words]

    if not words:
        return []

    # Search with the most specific terms
    query = " ".join(words[:3])
    return search_files(query, max_results=3)


def format_drive_results(files: list[dict]) -> str:
    """Format Drive search results for display."""
    if not files:
        return "No matching files found in Drive."

    lines = []
    for f in files:
        lines.append(f"📄 {f['name']}")
        if f.get("webViewLink"):
            lines.append(f"   {f['webViewLink']}")
    return "\n".join(lines)
