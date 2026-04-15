"""Unified state store — single `state.json` for all Claudette state.

Consolidates what used to live in seven separate files:
  memory.json, open_loops.json, preferences.json, scan_state.json,
  digest_loops.json, last_scheduler_messages.json, interactions.json.

Other modules (memory.py, open_loops.py, etc.) call into this module to
read/write their section. See docs/unified-state-plan.md for the full plan.

Step 1 is plumbing-only: no behavior change. Old files are migrated on
first load, then deleted. Writes are journaled (.tmp → fsync → rename)
and the last three versions are kept as rolling backups.
"""

import fcntl
import json
import logging
import os
import shutil
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_DIR, "state.json")
BACKUP_COUNT = 3

# Paths of the legacy files consumed during one-shot migration
_LEGACY_FILES = {
    "memory":      os.path.join(PROJECT_DIR, "memory.json"),
    "loops":       os.path.join(PROJECT_DIR, "open_loops.json"),
    "preferences": os.path.join(PROJECT_DIR, "preferences.json"),
    "scan_state":  os.path.join(PROJECT_DIR, "scan_state.json"),
    "digest_loops":         os.path.join(PROJECT_DIR, "digest_loops.json"),
    "scheduler_messages":   os.path.join(PROJECT_DIR, "last_scheduler_messages.json"),
    "interactions":         os.path.join(PROJECT_DIR, "interactions.json"),
}


def _default_state() -> dict:
    # Note: `preferences` is absent by design. It existed as a transitional
    # section during Step 1 to hold migrated preferences.json contents, but
    # Step 2 folds its data into `rules.*` and `loops` and removes the
    # section outright. Keeping it out of the default shape means
    # _ensure_shape() won't resurrect an empty `preferences` after
    # rules.migrate_from_preferences() drops it.
    return {
        "version": 1,
        "narrative": {
            "memories": [],
            "summaries": {"weekly": [], "monthly": [], "yearly": []},
            "last_compaction": None,
            "last_review": None,
        },
        "rules": {"ingestion": [], "closure": [], "priority": []},
        "loops": [],
        "session": {
            "last_scheduler_messages": [],
            "digest_loop_numbers": {},
        },
        "pipeline": {
            "last_scan_at": None,
            "scanned_thread_ids": [],
        },
        "audit": [],
    }


# ---------------------------------------------------------------------------
# Legacy migration — runs once on first load when state.json is missing.
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Any:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {path} during migration: {e}")
        return None


def _migrate_from_legacy() -> dict:
    """Build the unified state object from whichever legacy files exist.

    Missing files are tolerated — their sections stay at defaults. Called
    exactly once, when `state.json` does not exist. Any legacy file
    consumed is deleted after `state.json` is written successfully.
    """
    state = _default_state()

    # memory.json → narrative
    mem = _read_json(_LEGACY_FILES["memory"])
    if isinstance(mem, dict):
        state["narrative"]["memories"] = mem.get("memories", [])
        summaries = mem.get("summaries") or {}
        state["narrative"]["summaries"] = {
            "weekly":  summaries.get("weekly", []),
            "monthly": summaries.get("monthly", []),
            "yearly":  summaries.get("yearly", []),
        }
        state["narrative"]["last_compaction"] = mem.get("last_compaction")
        state["narrative"]["last_review"] = mem.get("last_review")

    # open_loops.json → loops (a list of dicts)
    loops = _read_json(_LEGACY_FILES["loops"])
    if isinstance(loops, list):
        state["loops"] = loops

    # preferences.json → transitional `preferences` section. Step 2's
    # rules.migrate_from_preferences() reads this and folds it into
    # rules.* / loops, then drops the section.
    prefs = _read_json(_LEGACY_FILES["preferences"])
    if isinstance(prefs, dict):
        state["preferences"] = prefs

    # scan_state.json → pipeline
    scan = _read_json(_LEGACY_FILES["scan_state"])
    if isinstance(scan, dict):
        state["pipeline"]["last_scan_at"] = scan.get("last_scan_at")
        state["pipeline"]["scanned_thread_ids"] = scan.get("scanned_thread_ids", [])

    # digest_loops.json → session.digest_loop_numbers
    digest_loops = _read_json(_LEGACY_FILES["digest_loops"])
    if isinstance(digest_loops, dict):
        # Keep keys as strings on disk (they're JSON numbers serialized as strings)
        state["session"]["digest_loop_numbers"] = {str(k): v for k, v in digest_loops.items()}

    # last_scheduler_messages.json → session.last_scheduler_messages
    msgs = _read_json(_LEGACY_FILES["scheduler_messages"])
    if isinstance(msgs, list):
        state["session"]["last_scheduler_messages"] = msgs

    # interactions.json → audit
    interactions = _read_json(_LEGACY_FILES["interactions"])
    if isinstance(interactions, list):
        state["audit"] = interactions

    return state


def _delete_legacy_files():
    """Remove the legacy JSON files after a successful state.json write."""
    for path in _LEGACY_FILES.values():
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"Removed legacy file {os.path.basename(path)}")
            except OSError as e:
                logger.warning(f"Could not remove {path}: {e}")


# ---------------------------------------------------------------------------
# Load / save with journaling, rolling backups, and cross-process locking.
# ---------------------------------------------------------------------------

_LOCK_PATH = STATE_FILE + ".lock"


class _FileLock:
    """Advisory cross-process lock held for the duration of a save.

    The scheduler and the bot run in separate processes and both write to
    state.json. flock serializes writes so a race can't produce a half-
    merged file; it does not prevent lost-update semantics if both
    processes loaded the same snapshot before either saved.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    def __enter__(self):
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _rotate_backups():
    """state.json → .bak.1 → .bak.2 → .bak.3 (oldest dropped)."""
    if not os.path.exists(STATE_FILE):
        return
    # Shift existing backups
    for i in range(BACKUP_COUNT, 1, -1):
        src = f"{STATE_FILE}.bak.{i - 1}"
        dst = f"{STATE_FILE}.bak.{i}"
        if os.path.exists(src):
            shutil.move(src, dst)
    # Current → bak.1
    shutil.copy2(STATE_FILE, f"{STATE_FILE}.bak.1")


def load_state() -> dict:
    """Load state.json, migrating from legacy files on first call.

    Always returns a dict with every section present at the default shape,
    so callers can treat missing sections as empty without extra checks.
    """
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            return _ensure_shape(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"state.json unreadable ({e}); falling back to backups")
            for i in range(1, BACKUP_COUNT + 1):
                bak = f"{STATE_FILE}.bak.{i}"
                if os.path.exists(bak):
                    try:
                        with open(bak) as f:
                            data = json.load(f)
                        logger.warning(f"Recovered state from {os.path.basename(bak)}")
                        return _ensure_shape(data)
                    except (json.JSONDecodeError, OSError):
                        continue
            logger.error("No readable backups; starting from default state")
            return _default_state()

    # First boot: migrate if any legacy file is present, then write atomically.
    legacy_present = any(os.path.exists(p) for p in _LEGACY_FILES.values())
    if legacy_present:
        logger.info("state.json missing; migrating from legacy files")
        migrated = _migrate_from_legacy()
        save_state(migrated)
        _delete_legacy_files()
        return migrated

    # Truly fresh install
    state = _default_state()
    save_state(state)
    return state


def _ensure_shape(data: dict) -> dict:
    """Fill in any missing top-level sections with defaults, so the rest
    of the codebase can assume a stable schema."""
    default = _default_state()
    for key, default_val in default.items():
        if key not in data:
            data[key] = default_val
        elif isinstance(default_val, dict) and isinstance(data[key], dict):
            for sub, sub_default in default_val.items():
                if sub not in data[key]:
                    data[key][sub] = sub_default
    return data


def save_state(state: dict):
    """Journaled write: tmp file → fsync → rename. Keeps 3 rolling backups."""
    with _FileLock(_LOCK_PATH):
        _rotate_backups()
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Section accessors — what the other modules actually call.
# ---------------------------------------------------------------------------

def get_section(name: str) -> Any:
    """Return a deep copy of `state[name]` so callers can mutate it freely
    without aliasing the on-disk object."""
    state = load_state()
    section = state.get(name)
    return json.loads(json.dumps(section, default=str)) if section is not None else None


def set_section(name: str, value: Any):
    """Replace `state[name]` and persist. Atomic at the whole-file level."""
    state = load_state()
    state[name] = value
    save_state(state)


# ---------------------------------------------------------------------------
# Prune hook — currently a no-op; real per-section hygiene lives in the
# individual modules (memory.py's caps, open_loops.py's expiry, etc.).
# The unified plan will centralize these here over time.
# ---------------------------------------------------------------------------

def prune():
    """Placeholder for future centralized hygiene.

    Per-section pruning is still owned by the individual modules today
    (memory caps, loop expiry, scan-state 500-ID cap). Step 1 leaves that
    behavior untouched — this function just exists so future steps can
    wire in centralized cleanup without churning callers.
    """
    return None
