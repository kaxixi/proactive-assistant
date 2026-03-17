"""Learning system — stores and applies user feedback to improve future digests."""

import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PREFS_FILE = os.path.join(PROJECT_DIR, "preferences.json")

DEFAULT_PREFS = {
    "rules": [],
    "senders_always_flag": [],
    "senders_never_flag": [],
    "dismissed_threads": [],
    "feedback_log": [],
}


def load_preferences() -> dict:
    if os.path.exists(PREFS_FILE):
        with open(PREFS_FILE) as f:
            return json.load(f)
    return DEFAULT_PREFS.copy()


def save_preferences(prefs: dict):
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2, default=str)


def add_rule(rule: str):
    """Add a learned preference rule."""
    prefs = load_preferences()
    if rule not in prefs["rules"]:
        prefs["rules"].append(rule)
        save_preferences(prefs)
        logger.info(f"Added preference rule: {rule}")


def add_sender_always_flag(email: str):
    prefs = load_preferences()
    if email not in prefs["senders_always_flag"]:
        prefs["senders_always_flag"].append(email)
        save_preferences(prefs)


def add_sender_never_flag(email: str):
    prefs = load_preferences()
    if email not in prefs["senders_never_flag"]:
        prefs["senders_never_flag"].append(email)
        save_preferences(prefs)


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
    """Return dismissed threads as text for the digest prompt, so Claude can judge
    whether new emails from the same sender/topic are truly new or duplicates."""
    prefs = load_preferences()
    dismissed = prefs.get("dismissed_threads", [])
    now = datetime.now(timezone.utc)
    active = []
    for d in dismissed:
        dismissed_at = datetime.fromisoformat(d["dismissed_at"])
        if (now - dismissed_at).days < 30:
            active.append(d)
    if not active:
        return ""
    lines = []
    for d in active:
        days_ago = (now - datetime.fromisoformat(d["dismissed_at"])).days
        lines.append(f"- [thread:{d.get('thread_id', '?')}] \"{d.get('subject', 'unknown')}\" — dismissed {days_ago}d ago (reason: {d.get('reason', 'handled')})")
    return "\n".join(lines)


def log_feedback(feedback_type: str, detail: str):
    """Log user feedback for future analysis."""
    prefs = load_preferences()
    prefs["feedback_log"].append({
        "type": feedback_type,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_preferences(prefs)
