"""Open loops — topic-level grouping of email threads for digest tracking."""

import json
import os
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

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
    snoozed_until: str | None = None
    snooze_count: int = 0


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


def loop_age_days(loop: OpenLoop) -> int:
    """Days since the loop was first opened (created_at).

    Use this for user-facing age, not loop.age_days — the latter resets
    to 0 whenever a fresh email arrives in the loop, which is misleading
    for topics that have been hanging around for a while.
    """
    if not loop.created_at:
        return loop.age_days
    try:
        created = datetime.fromisoformat(loop.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - created).days)
    except (ValueError, TypeError):
        return loop.age_days


def _is_snoozed(loop: OpenLoop) -> bool:
    """Check if a loop is currently snoozed."""
    if not loop.snoozed_until:
        return False
    try:
        snooze_end = datetime.fromisoformat(loop.snoozed_until)
        if snooze_end.tzinfo is None:
            snooze_end = snooze_end.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < snooze_end
    except (ValueError, TypeError):
        return False


def get_open_loops() -> list[OpenLoop]:
    """Return non-dismissed, non-expired, non-snoozed loops."""
    loops = load_loops()
    active = [l for l in loops if l.status == "open" and not _is_expired(l) and not _is_snoozed(l)]
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
    """Dismiss a loop by ID. Also clears matching follow-up memories.
    Returns the dismissed loop or None."""
    loops = load_loops()
    for loop in loops:
        if loop.loop_id == loop_id:
            loop.status = "dismissed"
            loop.dismissed_at = _now_iso()
            loop.dismiss_reason = reason
            loop.updated_at = _now_iso()
            save_loops(loops)
            logger.info(f"Dismissed loop {loop_id}: {loop.title} ({reason})")
            # Clear follow-up memories that match this loop's tags
            if loop.tags:
                try:
                    from memory import clear_follow_ups_by_tags
                    clear_follow_ups_by_tags(loop.tags)
                except Exception as e:
                    logger.warning(f"Failed to clear follow-ups for loop {loop_id}: {e}")
            return loop
    return None


def snooze_loop(loop_id: str, days: int = 2) -> OpenLoop | None:
    """Snooze a loop for N days. Returns the loop or None."""
    loops = load_loops()
    for loop in loops:
        if loop.loop_id == loop_id:
            loop.snoozed_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            loop.snooze_count += 1
            loop.updated_at = _now_iso()
            save_loops(loops)
            logger.info(f"Snoozed loop {loop_id}: {loop.title} for {days} days (count: {loop.snooze_count})")
            return loop
    return None


def get_loop_by_id(loop_id: str) -> OpenLoop | None:
    """Find a loop by ID regardless of status."""
    loops = load_loops()
    for loop in loops:
        if loop.loop_id == loop_id:
            return loop
    return None


def get_all_loop_thread_ids() -> set[str]:
    """Return ALL thread IDs across ALL loops (any status, including expired).

    Used by incremental scanning to determine which threads are already
    accounted for in the loop system.
    """
    loops = load_loops()
    ids = set()
    for l in loops:
        ids.update(l.thread_ids)
    return ids


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
    """Find an open loop matching a search query.

    Matches individual query words against loop fields. Requires at least
    2 words to match (or 1 word if the query is a single word). Scores
    by how many query words hit and where they hit (title > sender > tag).
    """
    query_lower = query.lower()
    query_words = [w for w in query_lower.split() if len(w) >= 3]
    if not query_words:
        return None

    open_loops = get_open_loops()
    best_match = None
    best_score = 0

    for loop in open_loops:
        title_lower = loop.title.lower()
        senders_lower = " ".join(loop.senders).lower()
        tags_lower = " ".join(
            tag.split(":", 1)[-1] if ":" in tag else tag
            for tag in loop.tags
        ).lower()
        summary_lower = loop.summary.lower()

        score = 0
        words_matched = 0

        for word in query_words:
            word_score = 0
            if word in title_lower:
                word_score = 10
            elif word in senders_lower:
                word_score = 8
            elif word in tags_lower:
                word_score = 5
            elif word in summary_lower:
                word_score = 3
            if word_score > 0:
                score += word_score
                words_matched += 1

        # Full query as phrase in title is a strong signal
        if query_lower in title_lower:
            score += 15

        # Require at least 2 matching words for multi-word queries
        # to avoid false positives from single-word tag overlaps
        min_words = min(2, len(query_words))
        if words_matched < min_words:
            continue

        if score > best_score:
            best_score = score
            best_match = loop

    return best_match
