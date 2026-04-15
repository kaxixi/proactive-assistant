"""Interaction tracking — records button presses and detects behavioral patterns."""

import logging
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

import state

logger = logging.getLogger(__name__)


@dataclass
class InteractionEvent:
    event_type: str        # "handled" | "snoozed" | "tell_me_more"
    loop_id: str
    loop_title: str
    tags: list[str]
    sender_domains: list[str]
    timestamp: str


def _load_interactions() -> list[dict]:
    return state.get_section("audit") or []


def _save_interactions(events: list[dict]):
    state.set_section("audit", events)


def record_interaction(event: InteractionEvent):
    """Record a user interaction event."""
    events = _load_interactions()
    events.append(asdict(event))
    # Keep only last 90 days of events
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    events = [e for e in events if e.get("timestamp", "") > cutoff]
    _save_interactions(events)
    logger.info(f"Recorded interaction: {event.event_type} on '{event.loop_title}'")


def get_interactions(event_type: str = None, days_back: int = 30) -> list[InteractionEvent]:
    """Get recent interactions, optionally filtered by type."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    events = _load_interactions()
    result = []
    for e in events:
        if e.get("timestamp", "") < cutoff:
            continue
        if event_type and e.get("event_type") != event_type:
            continue
        try:
            result.append(InteractionEvent(**e))
        except TypeError:
            continue
    return result


def _extract_domains(senders: list[str]) -> list[str]:
    """Extract email domains from sender addresses."""
    domains = []
    for s in senders:
        if "@" in s:
            domain = s.split("@")[-1].lower().strip(">")
            domains.append(domain)
    return domains


def pattern_hash(pattern_type: str, value: str) -> str:
    """Generate a short hash for a pattern (used in callback data)."""
    return hashlib.md5(f"{pattern_type}:{value}".encode()).hexdigest()[:8]


def detect_patterns(min_count: int = 3) -> list[dict]:
    """Detect repeated behavioral patterns from interaction history.

    Returns patterns where the user has taken the same action 3+ times
    on similar items, spanning at least 7 days.
    """
    events = get_interactions(days_back=60)
    actionable = [e for e in events if e.event_type in ("handled", "snoozed")]

    if len(actionable) < min_count:
        return []

    # Check existing preference memories to avoid re-suggesting
    try:
        from memory import get_preference_memories
        existing_prefs = {m["content"].lower() for m in get_preference_memories()}
    except Exception:
        existing_prefs = set()

    patterns = []

    # Group by sender domain
    domain_actions: dict[str, list[InteractionEvent]] = {}
    for e in actionable:
        for domain in e.sender_domains:
            domain_actions.setdefault(domain, []).append(e)

    for domain, events_list in domain_actions.items():
        if len(events_list) < min_count:
            continue
        # Check time span (7+ days)
        timestamps = sorted(e.timestamp for e in events_list)
        try:
            first = datetime.fromisoformat(timestamps[0])
            last = datetime.fromisoformat(timestamps[-1])
            if (last - first).days < 7:
                continue
        except (ValueError, TypeError):
            continue
        # Skip if already a preference
        if any(domain in p for p in existing_prefs):
            continue
        patterns.append({
            "pattern_type": "sender_domain",
            "value": domain,
            "action": "handled",
            "count": len(events_list),
            "hash": pattern_hash("sender_domain", domain),
            "description": f"You've dismissed {len(events_list)} emails from {domain}",
        })

    # Group by topic tags
    tag_actions: dict[str, list[InteractionEvent]] = {}
    for e in actionable:
        for tag in e.tags:
            if tag.startswith("topic:"):
                tag_actions.setdefault(tag, []).append(e)

    for tag, events_list in tag_actions.items():
        if len(events_list) < min_count:
            continue
        timestamps = sorted(e.timestamp for e in events_list)
        try:
            first = datetime.fromisoformat(timestamps[0])
            last = datetime.fromisoformat(timestamps[-1])
            if (last - first).days < 7:
                continue
        except (ValueError, TypeError):
            continue
        topic = tag.split(":", 1)[1]
        if any(topic in p for p in existing_prefs):
            continue
        patterns.append({
            "pattern_type": "topic",
            "value": topic,
            "action": "handled",
            "count": len(events_list),
            "hash": pattern_hash("topic", topic),
            "description": f"You've dismissed {len(events_list)} '{topic}' emails",
        })

    # Check for declined patterns (don't re-suggest for 30 days)
    try:
        from memory import get_active_memories
        declined = [
            m for m in get_active_memories()
            if m["type"] == "fact" and "declined auto-deprioritize" in m["content"].lower()
        ]
        declined_values = {m["content"].lower() for m in declined}
        patterns = [
            p for p in patterns
            if not any(p["value"].lower() in d for d in declined_values)
        ]
    except Exception:
        pass

    return patterns[:3]  # max 3 suggestions at a time
