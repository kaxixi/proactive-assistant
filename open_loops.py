"""Open loops — topic-level grouping of email threads for digest tracking."""

import json
import os
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOOPS_FILE = os.path.join(PROJECT_DIR, "open_loops.json")


@dataclass
class OpenLoop:
    loop_id: str
    title: str              # "Arjun's HCRP application"
    summary: str            # 1-2 sentence description
    thread_ids: list[str]   # Gmail thread IDs in this loop
    senders: list[str]      # All sender emails involved
    urgency: str            # max urgency of member threads
    age_days: int           # age of oldest unreplied email
    reason: str             # "unreplied" / "needs_followup" / "mixed"
    snippets: list[str]     # 2-3 recent snippets for context
    status: str = "open"    # "open" / "dismissed"
    tags: list[str] = field(default_factory=list)  # person:/topic: tags
    created_at: str = ""
    updated_at: str = ""
    dismissed_at: str | None = None
    dismiss_reason: str = ""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_loops() -> list[OpenLoop]:
    """Load all loops from disk."""
    if not os.path.exists(LOOPS_FILE):
        return []
    try:
        with open(LOOPS_FILE) as f:
            data = json.load(f)
        return [OpenLoop(**entry) for entry in data]
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Failed to load open_loops.json: {e}")
        return []


def save_loops(loops: list[OpenLoop]):
    """Persist all loops to disk."""
    with open(LOOPS_FILE, "w") as f:
        json.dump([asdict(loop) for loop in loops], f, indent=2, default=str)


EXPIRY_DAYS = 30  # loops expire after 30 days without activity


def _is_expired(loop: OpenLoop) -> bool:
    """Check if a loop has expired based on updated_at or dismissed_at."""
    now = datetime.now(timezone.utc)
    try:
        if loop.status == "dismissed" and loop.dismissed_at:
            ref = datetime.fromisoformat(loop.dismissed_at)
        else:
            ref = datetime.fromisoformat(loop.updated_at)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return (now - ref).days >= EXPIRY_DAYS
    except (ValueError, TypeError):
        return False


def get_open_loops() -> list[OpenLoop]:
    """Return non-dismissed, non-expired loops (open or follow_up)."""
    loops = load_loops()
    active = [l for l in loops if l.status == "open" and not _is_expired(l)]
    # Clean up expired loops from disk
    remaining = [l for l in loops if not _is_expired(l)]
    if len(remaining) != len(loops):
        expired_count = len(loops) - len(remaining)
        save_loops(remaining)
        logger.info(f"Expired {expired_count} stale loop(s)")
    return active


def get_loop_thread_ids(status: str = "dismissed") -> set[str]:
    """Return all thread IDs from non-expired loops with the given status."""
    loops = load_loops()
    ids = set()
    for l in loops:
        if l.status == status and not _is_expired(l):
            ids.update(l.thread_ids)
    return ids


def dismiss_loop(loop_id: str, reason: str) -> OpenLoop | None:
    """Dismiss a loop by ID. Returns the dismissed loop or None."""
    loops = load_loops()
    for loop in loops:
        if loop.loop_id == loop_id:
            loop.status = "dismissed"
            loop.dismissed_at = _now_iso()
            loop.dismiss_reason = reason
            loop.updated_at = _now_iso()
            save_loops(loops)
            logger.info(f"Dismissed loop {loop_id}: {loop.title} ({reason})")
            return loop
    return None


def upsert_loops(new_loops: list[OpenLoop]):
    """Create or update loops from the grouping step.

    Matches by loop_id. New loops are appended, existing loops get
    their thread_ids, senders, snippets, urgency, and age_days updated.
    """
    existing = load_loops()
    existing_by_id = {l.loop_id: l for l in existing}

    for nl in new_loops:
        if nl.loop_id in existing_by_id:
            el = existing_by_id[nl.loop_id]
            # Merge thread IDs and senders
            el.thread_ids = list(dict.fromkeys(el.thread_ids + nl.thread_ids))
            el.senders = list(dict.fromkeys(el.senders + nl.senders))
            el.snippets = nl.snippets  # use latest snippets
            el.urgency = nl.urgency
            el.age_days = nl.age_days
            el.reason = nl.reason
            el.summary = nl.summary
            el.tags = list(dict.fromkeys(el.tags + nl.tags))
            el.updated_at = _now_iso()
        else:
            nl.created_at = nl.created_at or _now_iso()
            nl.updated_at = _now_iso()
            existing.append(nl)

    save_loops(existing)
    logger.info(f"Upserted loops: {len(new_loops)} processed, {len(existing)} total on disk")


def find_loop_by_query(query: str) -> OpenLoop | None:
    """Find an open loop matching a search query (fuzzy match against title, senders, tags)."""
    query_lower = query.lower()
    open_loops = get_open_loops()

    # Score each loop
    best_match = None
    best_score = 0

    for loop in open_loops:
        score = 0
        # Title match
        if query_lower in loop.title.lower():
            score += 10
        # Sender match
        for sender in loop.senders:
            if query_lower in sender.lower():
                score += 8
        # Tag match
        for tag in loop.tags:
            tag_value = tag.split(":", 1)[-1].lower() if ":" in tag else tag.lower()
            if query_lower in tag_value or tag_value in query_lower:
                score += 6
        # Summary match
        if query_lower in loop.summary.lower():
            score += 3

        if score > best_score:
            best_score = score
            best_match = loop

    return best_match if best_score > 0 else None
