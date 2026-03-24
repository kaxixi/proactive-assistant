"""Scan state — tracks incremental email scanning progress."""

import json
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_DIR, "scan_state.json")


def load_scan_state() -> dict:
    """Load scan state from disk.

    Returns dict with keys:
        last_scan_at: str (ISO timestamp) or None
        scanned_thread_ids: list[str] — thread IDs seen but NOT in any loop
    """
    if not os.path.exists(STATE_FILE):
        return {"last_scan_at": None, "scanned_thread_ids": []}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {
            "last_scan_at": data.get("last_scan_at"),
            "scanned_thread_ids": data.get("scanned_thread_ids", []),
        }
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to load scan_state.json: {e}")
        return {"last_scan_at": None, "scanned_thread_ids": []}


def save_scan_state(state: dict):
    """Persist scan state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_last_scan_time() -> str | None:
    """Return ISO timestamp of last scan, or None (triggers backfill)."""
    return load_scan_state()["last_scan_at"]


def get_scanned_thread_ids() -> set[str]:
    """Return set of thread IDs that were scanned but not assigned to any loop."""
    return set(load_scan_state()["scanned_thread_ids"])


def update_after_scan(
    scan_timestamp: str,
    new_thread_ids: list[str],
    loop_assigned_ids: set[str],
):
    """Update state after a scan completes.

    Args:
        scan_timestamp: ISO timestamp of when this scan started
        new_thread_ids: all thread_ids returned by Gmail in this scan
        loop_assigned_ids: thread IDs that are in any loop (any status)

    Unassigned IDs (scanned but not in loops) go into scanned_thread_ids
    so they won't be re-processed. Capped at 500 to prevent unbounded growth.
    """
    state = load_scan_state()
    state["last_scan_at"] = scan_timestamp

    existing = set(state["scanned_thread_ids"])
    unassigned = set(new_thread_ids) - loop_assigned_ids
    existing.update(unassigned)

    # Remove any IDs that are now in loops
    existing -= loop_assigned_ids

    # Cap to prevent unbounded growth
    if len(existing) > 500:
        existing = set(list(existing)[-500:])

    state["scanned_thread_ids"] = list(existing)
    save_scan_state(state)
    logger.info(
        f"Scan state updated: last_scan_at={scan_timestamp}, "
        f"{len(unassigned)} new unassigned, {len(existing)} total tracked"
    )
