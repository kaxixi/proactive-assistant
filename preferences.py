"""Learning system — stores and applies user feedback to improve future digests."""

import logging
from datetime import datetime, timezone

import state

logger = logging.getLogger(__name__)

DEFAULT_PREFS = {
    "dismissed_threads": [],
    "senders_always_flag": [],
    "senders_never_flag": [],
    "feedback_log": [],
    "rules": [],
}


def load_preferences() -> dict:
    return state.get_section("preferences") or DEFAULT_PREFS.copy()


def save_preferences(prefs: dict):
    state.set_section("preferences", prefs)



def dismiss_thread(thread_id: str, subject: str = "", reason: str = ""):
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


