"""Learning system — stores and applies user feedback to improve future digests."""

import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PREFS_FILE = os.path.join(PROJECT_DIR, "preferences.json")

DEFAULT_PREFS = {
    "dismissed_threads": [],
}


def load_preferences() -> dict:
    if os.path.exists(PREFS_FILE):
        with open(PREFS_FILE) as f:
            return json.load(f)
    return DEFAULT_PREFS.copy()


def save_preferences(prefs: dict):
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2, default=str)



def dismiss_thread(thread_id: str, subject: str = "", reason: str = "", sender_email: str = ""):
    """Dismiss a thread so it won't appear in future digests."""
    prefs = load_preferences()
    dismissed = prefs.get("dismissed_threads", [])
    # Don't add duplicates
    if any(d["thread_id"] == thread_id for d in dismissed):
        return
    entry = {
        "thread_id": thread_id,
        "subject": subject,
        "reason": reason,
        "dismissed_at": datetime.now(timezone.utc).isoformat(),
    }
    if sender_email:
        entry["sender_email"] = sender_email.lower()
    dismissed.append(entry)
    prefs["dismissed_threads"] = dismissed
    save_preferences(prefs)
    logger.info(f"Dismissed thread {thread_id}: {subject} ({reason})")


def get_dismissed_thread_ids() -> set:
    """Return set of dismissed thread IDs, auto-expiring after 30 days."""
    prefs = load_preferences()
    dismissed = prefs.get("dismissed_threads", [])
    now = datetime.now(timezone.utc)
    active = []
    ids = set()
    for d in dismissed:
        dismissed_at = datetime.fromisoformat(d["dismissed_at"])
        if (now - dismissed_at).days < 30:
            active.append(d)
            ids.add(d["thread_id"])
    # Clean up expired entries
    if len(active) != len(dismissed):
        prefs["dismissed_threads"] = active
        save_preferences(prefs)
    return ids


def get_dismissed_context() -> str:
    """Return dismissed threads and loops as text for the digest prompt, so Claude can judge
    whether new emails from the same sender/topic are truly new or duplicates."""
    now = datetime.now(timezone.utc)
    lines = []

    # Legacy dismissed threads from preferences.json
    prefs = load_preferences()
    dismissed = prefs.get("dismissed_threads", [])
    for d in dismissed:
        dismissed_at = datetime.fromisoformat(d["dismissed_at"])
        if (now - dismissed_at).days < 30:
            days_ago = (now - dismissed_at).days
            lines.append(f"- [thread:{d.get('thread_id', '?')}] \"{d.get('subject', 'unknown')}\" — dismissed {days_ago}d ago (reason: {d.get('reason', 'handled')})")

    # Dismissed loops from open_loops.json
    try:
        from open_loops import load_loops
        for loop in load_loops():
            if loop.status != "dismissed" or not loop.dismissed_at:
                continue
            dismissed_at = datetime.fromisoformat(loop.dismissed_at)
            if (now - dismissed_at).days < 30:
                days_ago = (now - dismissed_at).days
                lines.append(
                    f"- [loop:{loop.loop_id}] \"{loop.title}\" ({len(loop.thread_ids)} threads) "
                    f"— dismissed {days_ago}d ago (reason: {loop.dismiss_reason or 'handled'})"
                )
    except Exception:
        pass  # open_loops.json may not exist yet

    return "\n".join(lines) if lines else ""


