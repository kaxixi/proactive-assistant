"""Dropbox integration — search and retrieve files."""

import logging
import dropbox

from config import DROPBOX_ACCESS_TOKEN

logger = logging.getLogger(__name__)


def _get_client():
    return dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)


def search_files(query: str, max_results: int = 5) -> list[dict]:
    """Search Dropbox for files matching a query.

    Returns list of dicts with: name, path, modified, size
    """
    try:
        dbx = _get_client()
        result = dbx.files_search_v2(query)

        files = []
        for match in result.matches[:max_results]:
            metadata = match.metadata.get_metadata()
            if hasattr(metadata, "client_modified"):
                files.append({
                    "name": metadata.name,
                    "path": metadata.path_display,
                    "modified": str(metadata.client_modified),
                    "size": metadata.size,
                })
            else:
                # Folder
                files.append({
                    "name": metadata.name,
                    "path": metadata.path_display,
                    "modified": "",
                    "size": 0,
                })

        logger.info(f"Dropbox search '{query}': found {len(files)} files")
        return files
    except Exception as e:
        logger.warning(f"Dropbox search failed: {e}")
        return []


def get_recent_files(max_results: int = 10) -> list[dict]:
    """Get recently modified files from Dropbox."""
    try:
        dbx = _get_client()
        # List the root and sort by modified
        result = dbx.files_list_folder("", recursive=True, limit=100)

        files = []
        for entry in result.entries:
            if hasattr(entry, "client_modified"):
                files.append({
                    "name": entry.name,
                    "path": entry.path_display,
                    "modified": str(entry.client_modified),
                    "size": entry.size,
                })

        # Sort by modified date descending
        files.sort(key=lambda f: f["modified"], reverse=True)
        return files[:max_results]
    except Exception as e:
        logger.warning(f"Dropbox recent files failed: {e}")
        return []


def find_files_for_meeting(meeting_summary: str) -> list[dict]:
    """Find Dropbox files that might be relevant to a meeting."""
    skip_words = {"the", "and", "for", "with", "meeting", "call", "chat",
                  "sync", "check", "weekly", "daily", "monthly", "update"}
    words = [w for w in meeting_summary.lower().split()
             if len(w) > 2 and w not in skip_words]

    if not words:
        return []

    query = " ".join(words[:3])
    return search_files(query, max_results=3)


def format_dropbox_results(files: list[dict]) -> str:
    """Format Dropbox search results for display."""
    if not files:
        return "No matching files found in Dropbox."

    lines = []
    for f in files:
        size_mb = f["size"] / (1024 * 1024) if f["size"] else 0
        lines.append(f"📁 {f['name']}")
        lines.append(f"   {f['path']}")
        if size_mb > 0.1:
            lines.append(f"   {size_mb:.1f} MB — modified {f['modified']}")
    return "\n".join(lines)
