"""Open loops — topic-level grouping of email threads for digest tracking."""

import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

import state

logger = logging.getLogger(__name__)


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
    """Load all loops from the unified state."""
    data = state.get_section("loops") or []
    loops = []
    for entry in data:
        try:
            loops.append(OpenLoop(**entry))
        except TypeError as e:
            logger.warning(f"Skipping malformed loop entry: {e}")
    return loops


def save_loops(loops: list[OpenLoop]):
    """Persist all loops to the unified state."""
    state.set_section("loops", [asdict(loop) for loop in loops])


OPEN_EXPIRY_DAYS = 30       # open loops expire after 30 days without activity
DISMISSED_RETENTION_DAYS = 90  # dismissed loops kept 90 days for pattern detection
# Backwards-compat alias for anything importing the old name
EXPIRY_DAYS = OPEN_EXPIRY_DAYS


def _is_expired(loop: OpenLoop) -> bool:
    """Check whether a loop should be dropped from the store.

    Open loops age out after OPEN_EXPIRY_DAYS of inactivity (measured
    from updated_at). Dismissed loops are kept for
    DISMISSED_RETENTION_DAYS so pattern detection and digest
    context formatting can reference them.
    """
    now = datetime.now(timezone.utc)
    try:
        if loop.status == "dismissed" and loop.dismissed_at:
            ref = datetime.fromisoformat(loop.dismissed_at)
            cutoff = DISMISSED_RETENTION_DAYS
        else:
            ref = datetime.fromisoformat(loop.updated_at)
            cutoff = OPEN_EXPIRY_DAYS
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return (now - ref).days >= cutoff
    except (ValueError, TypeError):
        return False


def prune() -> dict:
    """Drop loops that have aged past their retention window. See
    `_is_expired` for the split between open (30d) and dismissed (90d)."""
    loops = load_loops()
    kept = [l for l in loops if not _is_expired(l)]
    dropped = len(loops) - len(kept)
    if dropped:
        save_loops(kept)
    return {"loops_dropped": dropped, "loops_kept": len(kept)}


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


def dismiss_thread_as_loop(
    thread_id: str,
    subject: str = "",
    sender: str = "",
    reason: str = "",
    tags: list[str] | None = None,
    dismissed_at: str | None = None,
) -> OpenLoop:
    """Record an ad-hoc thread dismissal as a single-thread dismissed loop.

    Used by the bot's Gmail-fallback dismissal path (for threads that don't
    match any existing loop) and by the one-shot migration from
    preferences.dismissed_threads. Keeps the "one store for loops"
    invariant: every dismissed thread lives in the loops list.
    """
    now = _now_iso()
    loop = OpenLoop(
        loop_id=_new_id(),
        title=subject or "(no subject)",
        summary=f"Ad-hoc thread dismissal: {reason}" if reason else "Ad-hoc thread dismissal",
        thread_ids=[thread_id],
        senders=[sender] if sender else [],
        urgency="low",
        age_days=0,
        reason="dismissed",
        snippets=[],
        status="dismissed",
        tags=list(tags or []),
        created_at=dismissed_at or now,
        updated_at=now,
        dismissed_at=dismissed_at or now,
        dismiss_reason=reason or "ad-hoc dismissal",
    )
    loops = load_loops()
    loops.append(loop)
    save_loops(loops)
    logger.info(f"Ad-hoc thread dismissal stored as loop {loop.loop_id}: {subject}")
    return loop


def get_dismissed_context_text(window_days: int = 30) -> str:
    """Dismissed-loop context for the digest prompt, so Claude can judge
    whether a new email from the same sender/topic is truly new or a
    duplicate of something already handled. Takes over what the old
    preferences.get_dismissed_context used to produce."""
    now = datetime.now(timezone.utc)
    lines = []
    for loop in load_loops():
        if loop.status != "dismissed" or not loop.dismissed_at:
            continue
        try:
            dismissed_at = datetime.fromisoformat(loop.dismissed_at)
        except (ValueError, TypeError):
            continue
        if dismissed_at.tzinfo is None:
            dismissed_at = dismissed_at.replace(tzinfo=timezone.utc)
        days_ago = (now - dismissed_at).days
        if days_ago >= window_days:
            continue
        lines.append(
            f"- [loop:{loop.loop_id}] \"{loop.title}\" ({len(loop.thread_ids)} threads) "
            f"— dismissed {days_ago}d ago (reason: {loop.dismiss_reason or 'handled'})"
        )
    return "\n".join(lines)


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
