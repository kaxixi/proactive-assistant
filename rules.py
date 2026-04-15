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

import json
import logging
import re
import uuid
from datetime import datetime, timezone

import state

logger = logging.getLogger(__name__)


COMPILE_PROMPT = """You compile preference memories into structured ingestion rules.

You receive ONE preference memory. Decide whether it expresses a filter
rule about EMAIL SENDERS — i.e. "skip / ignore / don't flag emails from X"
or "always flag / always surface emails from X". If so, return a rule.
Otherwise return compile=false.

Only these shapes are allowed:

Ingestion SKIP rule (skip emails from a sender):
  {"compile": true, "kind": "ingestion", "match": {"sender": "<substring>"}, "action": "skip"}
or with a regex for precise matching:
  {"compile": true, "kind": "ingestion", "match": {"sender_regex": "<regex>"}, "action": "skip"}

Ingestion ALWAYS-FLAG rule:
  {"compile": true, "kind": "ingestion", "match": {"sender": "<substring>"}, "action": "always_flag"}

Do NOT compile if the preference is about something other than sender-based
filtering (e.g. meeting priorities, topic importance, non-email behavior,
scheduling). Those belong in narrative memory, not rules. Example of a
NON-compileable preference: "Grant-related emails are almost always urgent"
— that's about topic urgency, not sender filtering.

Match substrings should be specific enough to avoid false positives: prefer
a full email address ("noreply@vercel.com") over a generic word ("vercel").
Use sender_regex only when substring matching would overreach.

Return JSON only, no explanation, no markdown fences.

If compile is false: {"compile": false, "reason": "<one short sentence>"}

<preference>
{content}
</preference>"""


def compile_preference_to_rule(content: str, source_memory_id: str) -> dict | None:
    """Ask Claude to convert a preference memory into a structured rule.

    Returns the rule dict if Claude judges the preference is a sender
    filter, else None. Non-fatal on API/parse errors — returns None."""
    try:
        import anthropic
        from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": COMPILE_PROMPT.replace("{content}", content)}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        parsed = json.loads(raw)
        if not parsed.get("compile"):
            return None
        kind = parsed.get("kind")
        match = parsed.get("match") or {}
        action = parsed.get("action")
        if kind != "ingestion" or action not in ("skip", "always_flag"):
            logger.info(f"Compiler returned unsupported shape: {parsed}")
            return None
        if not (match.get("sender") or match.get("sender_regex")):
            logger.info(f"Compiler returned rule with empty sender match: {parsed}")
            return None
        rule = add_rule(
            kind=kind,
            match=match,
            action=action,
            source_memory_id=source_memory_id,
            confirmed=False,
            dry_run_count=3,
        )
        logger.info(
            f"Compiled preference into rule {rule['id']}: "
            f"match={match} action={action} (unconfirmed, dry_run=3)"
        )
        return rule
    except Exception as e:
        logger.warning(f"Rule compilation failed (non-fatal): {e}")
        return None


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


def remove_rules_by_source_memory(memory_ids: set[str]) -> int:
    """Drop every rule whose source_memory_id is in `memory_ids`.

    Used by `memory.forget_memories` so rules can't outlive the memory
    that produced them. Returns the count removed."""
    if not memory_ids:
        return 0
    section = _rules_section()
    removed = 0
    for kind, bucket in section.items():
        kept = []
        for rule in bucket:
            if rule.get("source_memory_id") in memory_ids:
                removed += 1
                logger.info(
                    f"Cascade-removing {kind} rule {rule.get('id')} "
                    f"(source memory forgotten)"
                )
            else:
                kept.append(rule)
        section[kind] = kept
    if removed:
        state.set_section("rules", section)
    return removed


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


def note_fire(rule_id: str, context: str = ""):
    """Increment fire_count and last_fired_at. If the rule is still
    unconfirmed and within its dry_run_count, also append an entry to
    the per-scan dry-run buffer so the digest can report the first
    few matches back to the user.

    No-op if rule vanished.
    """
    section = _rules_section()
    fired_rule = None
    for bucket in section.values():
        for rule in bucket:
            if rule.get("id") == rule_id:
                rule["fire_count"] = rule.get("fire_count", 0) + 1
                rule["last_fired_at"] = _now_iso()
                fired_rule = rule
                break
        if fired_rule:
            break
    if not fired_rule:
        return
    state.set_section("rules", section)

    if not fired_rule.get("confirmed") and fired_rule["fire_count"] <= fired_rule.get("dry_run_count", 0):
        session = state.get_section("session") or {}
        buffer = session.setdefault("dry_run_fires", [])
        buffer.append({
            "rule_id": fired_rule["id"],
            "match": fired_rule.get("match", {}),
            "action": fired_rule.get("action"),
            "context": context,
            "at": _now_iso(),
        })
        state.set_section("session", session)


def pop_dry_run_fires() -> list[dict]:
    """Drain and return the dry-run fire buffer."""
    session = state.get_section("session") or {}
    fires = session.get("dry_run_fires") or []
    if fires:
        session["dry_run_fires"] = []
        state.set_section("session", session)
    return fires


def describe_rule(rule: dict) -> str:
    """Human-friendly one-liner for a rule — used in digests and /rules."""
    match = rule.get("match", {})
    target = match.get("sender") or match.get("sender_regex") or "?"
    action = rule.get("action", "?")
    status = "confirmed" if rule.get("confirmed") else "unconfirmed"
    return f"[{rule['kind']}] {action} sender matching \"{target}\" ({status}, fired {rule.get('fire_count', 0)}x)"


def list_rules_text() -> str:
    """Human-readable listing of all rules, grouped by kind."""
    section = _rules_section()
    lines = []
    for kind in ("ingestion", "closure", "priority"):
        bucket = section.get(kind, [])
        if not bucket:
            continue
        lines.append(f"• {kind}:")
        for rule in bucket:
            lines.append(f"    {rule['id']}  {describe_rule(rule)}")
    if not lines:
        return "No rules set."
    return "\n".join(lines)


def get_unconfirmed_rules() -> list[dict]:
    """Return all rules that are still unconfirmed, across kinds."""
    out = []
    section = _rules_section()
    for bucket in section.values():
        for rule in bucket:
            if not rule.get("confirmed"):
                out.append(rule)
    return out


def confirm_rule(rule_id: str) -> dict | None:
    """Flip a rule to confirmed. Returns the rule dict or None if missing."""
    section = _rules_section()
    for bucket in section.values():
        for rule in bucket:
            if rule.get("id") == rule_id:
                rule["confirmed"] = True
                rule["dry_run_count"] = 0
                state.set_section("rules", section)
                logger.info(f"Confirmed rule {rule_id}")
                return rule
    return None


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


def sender_never_flagged(email_addr: str) -> dict | None:
    """If any ingestion rule with action='skip' matches this sender,
    return the rule dict so callers can record the fire. Returns None
    when no rule matches."""
    for rule in load_rules("ingestion"):
        if rule.get("action") == "skip" and _sender_match(rule.get("match", {}), email_addr):
            return rule
    return None


def sender_always_flagged(email_addr: str) -> dict | None:
    """If any ingestion rule with action='always_flag' matches this sender,
    return the rule dict. Returns None otherwise."""
    for rule in load_rules("ingestion"):
        if rule.get("action") == "always_flag" and _sender_match(rule.get("match", {}), email_addr):
            return rule
    return None


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
