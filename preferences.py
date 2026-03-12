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


def log_feedback(feedback_type: str, detail: str):
    """Log user feedback for future analysis."""
    prefs = load_preferences()
    prefs["feedback_log"].append({
        "type": feedback_type,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_preferences(prefs)
