"""Structured rules — compiled behavior that runs deterministically at scan time.

Unlike narrative memory (stored as preference strings that nudge LLM reasoning),
rules are structured predicates + actions the pipeline applies directly. They
live under `state.rules.{ingestion,closure,priority}` as lists of entries:

    {
      "id": "r_abc123",
      "source_memory_id": "migrated" | "<memory-id>",
      "kind": "ingestion" | "closure" | "priority",
      "match": { ... },          # structured predicate
      "action": "skip" | "always_flag" | "auto_close" | ...,
      "dry_run_count": 0,        # first N firings surfaced to user; 0 after confirmation
      "confirmed": False,        # flips True after user approves on first compile
      "last_fired_at": None,
      "fire_count": 0,
      "created_at": "..."
    }

Step 2 only uses `ingestion` kind with actions `skip` and `always_flag`,
migrated from the old preferences.senders_{never,always}_flag lists.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

import state

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return "r_" + uuid.uuid4().hex[:10]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rules_section() -> dict:
    section = state.get_section("rules") or {}
    for k in ("ingestion", "closure", "priority"):
        section.setdefault(k, [])
    return section


def load_rules(kind: str | None = None) -> list[dict]:
    """Return all rules of a given kind, or all rules if kind is None."""
    section = _rules_section()
    if kind is None:
        return section["ingestion"] + section["closure"] + section["priority"]
    return list(section.get(kind, []))


def add_rule(
    kind: str,
    match: dict,
    action: str,
    source_memory_id: str = "migrated",
    confirmed: bool = False,
    dry_run_count: int = 3,
) -> dict:
    """Add a new rule. Idempotent on (kind, match, action): existing
    identical rule is returned unchanged."""
    section = _rules_section()
    bucket = section.setdefault(kind, [])
    for existing in bucket:
        if existing.get("match") == match and existing.get("action") == action:
            return existing
    rule = {
        "id": _new_id(),
        "source_memory_id": source_memory_id,
        "kind": kind,
        "match": match,
        "action": action,
        "dry_run_count": dry_run_count,
        "confirmed": confirmed,
        "last_fired_at": None,
        "fire_count": 0,
        "created_at": _now_iso(),
    }
    bucket.append(rule)
    state.set_section("rules", section)
    logger.info(f"Added {kind} rule {rule['id']}: match={match} action={action}")
    return rule


def remove_rule(rule_id: str) -> bool:
    """Remove a rule by id. Returns True if one was removed."""
    section = _rules_section()
    for kind, bucket in section.items():
        for i, rule in enumerate(bucket):
            if rule.get("id") == rule_id:
                bucket.pop(i)
                state.set_section("rules", section)
                logger.info(f"Removed {kind} rule {rule_id}")
                return True
    return False


def note_fire(rule_id: str):
    """Increment fire_count and last_fired_at; no-op if rule vanished."""
    section = _rules_section()
    for kind, bucket in section.items():
        for rule in bucket:
            if rule.get("id") == rule_id:
                rule["fire_count"] = rule.get("fire_count", 0) + 1
                rule["last_fired_at"] = _now_iso()
                state.set_section("rules", section)
                return


# ---------------------------------------------------------------------------
# Ingestion-time sender lookups — the hot path email_monitor uses per scan.
# ---------------------------------------------------------------------------

def _sender_match(rule_match: dict, email_addr: str) -> bool:
    """A rule with match={'sender': '...'} matches the email via substring
    (case-insensitive). Domain-only matches ('@example.com') work too.
    Full regex is available via match={'sender_regex': '...'}."""
    sender_pat = rule_match.get("sender")
    if sender_pat and sender_pat.lower() in email_addr.lower():
        return True
    sender_re = rule_match.get("sender_regex")
    if sender_re:
        try:
            return re.search(sender_re, email_addr, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def sender_never_flagged(email_addr: str) -> bool:
    """True if any ingestion rule with action='skip' matches this sender."""
    for rule in load_rules("ingestion"):
        if rule.get("action") == "skip" and _sender_match(rule.get("match", {}), email_addr):
            return True
    return False


def sender_always_flagged(email_addr: str) -> bool:
    """True if any ingestion rule with action='always_flag' matches this sender."""
    for rule in load_rules("ingestion"):
        if rule.get("action") == "always_flag" and _sender_match(rule.get("match", {}), email_addr):
            return True
    return False


# ---------------------------------------------------------------------------
# One-shot migration from the legacy preferences section.
# ---------------------------------------------------------------------------

def migrate_from_preferences() -> int:
    """Fold `preferences.senders_never_flag` / `senders_always_flag` /
    `dismissed_threads` into rules + dismissed loops, then clear the
    preferences section.

    Idempotent: running it again on an already-migrated store is a no-op
    (empty preferences produce zero new rules).

    Returns the number of items migrated (rules + dismissed-thread loops).
    """
    prefs = state.get_section("preferences") or {}
    migrated = 0

    never_flag = prefs.get("senders_never_flag", []) or []
    for addr in never_flag:
        add_rule(
            kind="ingestion",
            match={"sender": addr},
            action="skip",
            source_memory_id="migrated",
            confirmed=True,
            dry_run_count=0,
        )
        migrated += 1

    always_flag = prefs.get("senders_always_flag", []) or []
    for addr in always_flag:
        add_rule(
            kind="ingestion",
            match={"sender": addr},
            action="always_flag",
            source_memory_id="migrated",
            confirmed=True,
            dry_run_count=0,
        )
        migrated += 1

    # dismissed_threads → single-thread dismissed loops so the "one store for
    # loops" invariant holds and the hard-filter path stays simple.
    dismissed_threads = prefs.get("dismissed_threads", []) or []
    if dismissed_threads:
        from open_loops import dismiss_thread_as_loop
        for d in dismissed_threads:
            thread_id = d.get("thread_id")
            if not thread_id:
                continue
            dismiss_thread_as_loop(
                thread_id=thread_id,
                subject=d.get("subject", ""),
                sender="",
                reason=d.get("reason") or "migrated from preferences",
                dismissed_at=d.get("dismissed_at"),
            )
            migrated += 1

    if migrated:
        logger.info(f"Migrated {migrated} entries from preferences to rules/loops")

    # Drop the preferences section outright — its contents are now owned by
    # rules.* and loops.
    cur = state.load_state()
    cur.pop("preferences", None)
    state.save_state(cur)

    return migrated
