"""Memory system — extracts and stores key facts from conversations and digests.

Hierarchical compaction keeps the store bounded over decades:
  Individual memories (this week) → weekly summaries → monthly → yearly.
Relationships and preferences are never compacted or expired.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(PROJECT_DIR, "memory.json")

# How long each memory type lasts before auto-expiry
EXPIRY_DAYS = {
    "resolved": 14,
    "pending": 30,
    "follow_up": None,  # no expiry — stays until explicitly resolved
    "relationship": None,  # no expiry
    "fact": 60,
    "preference": None,  # no expiry
    "conversation_summary": 14,
}

# Hard caps per type — oldest pruned when exceeded
TYPE_CAPS = {
    "preference": 30,
    "relationship": 40,
    "fact": 50,
    "resolved": 40,
    "pending": 30,
    "follow_up": 20,
    "conversation_summary": 20,
}

# Compactable types — these roll up into summaries. Others are permanent.
COMPACTABLE_TYPES = {"resolved", "fact", "conversation_summary"}

DEFAULT_MEMORY = {
    "memories": [],
    "summaries": {"weekly": [], "monthly": [], "yearly": []},
    "last_compaction": None,
}

EXTRACTION_PROMPT = """You are a memory extraction system for a personal assistant called Claudette.
Analyze the following interaction and extract key memories worth retaining.

For each memory, return a JSON array of objects with these fields:
- "type": one of "resolved", "pending", "follow_up", "relationship", "fact", "preference"
- "content": a concise, self-contained statement (one sentence)
- "tags": array of relevant tags (person names as "person:Name", or general tags like "email", "meeting", "travel", "deadline")

Type guidelines:
- "resolved": something completed, handled, or no longer needs attention
- "pending": a task or open item Erez still needs to act on (auto-expires after 30 days)
- "follow_up": something Erez explicitly asked to be reminded about until he says it's done. Use this when Erez says "keep reminding me", "don't let me forget", or "remind me again". These NEVER auto-expire — they stay active until Erez says it's resolved.
- "relationship": information about a person and their role/relationship to Erez
- "fact": a concrete fact about Erez's schedule, plans, or situation
- "preference": a lasting pattern about what Erez cares about, how he wants things handled,
  or what topics/senders are important or unimportant. Capture the *why* and *when* —
  e.g., "Grant-related emails are almost always urgent — Erez acts on these same-day"
  rather than just "Grant emails are important".
  Also extract importance patterns when you notice them:
  - Topics Erez consistently acts on quickly → high importance preference
  - Topics Erez repeatedly dismisses or ignores → low importance preference
  - Senders Erez treats as high-priority → sender importance preference

<already_handled>
{already_handled}
</already_handled>

CRITICAL: Do NOT create "pending" memories for items listed in <already_handled>. Creating pending items for resolved issues will cause the digest to re-flag them, which may confuse Erez into re-doing work he's already completed. If something appears in the handled list, it is DONE — record it as "resolved" if needed, never as "pending".

CRITICAL: When source is "digest", do NOT create "pending" memories at all. Email action items are already tracked as open loops — duplicating them as pending memories causes accumulation and clutter. From digests, only extract: relationships, facts, preferences, and resolved items.

<format>
Rules:
- Only extract genuinely useful information. Skip pleasantries and small talk.
- If nothing worth remembering happened, return an empty array [].
- Each memory must be self-contained — understandable without the original context.
- Keep each "content" field to ONE sentence.
- Return ONLY valid JSON. No explanation text.
</format>

<interaction source="{source}">
{conversation_text}
</interaction>"""


# ---------------------------------------------------------------------------
# Core load/save
# ---------------------------------------------------------------------------

def load_memories() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            data = json.load(f)
    else:
        data = DEFAULT_MEMORY.copy()
    # Migration: ensure summaries structure exists
    if "summaries" not in data:
        data["summaries"] = {"weekly": [], "monthly": [], "yearly": []}
    return data


def save_memories(data: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Add and retrieve memories
# ---------------------------------------------------------------------------

def clear_follow_ups_by_tags(tags: list):
    """Remove follow_up memories that match the given tags (used when a loop is dismissed)."""
    data = load_memories()
    before = len(data["memories"])
    data["memories"] = [
        m for m in data["memories"]
        if not (m["type"] == "follow_up" and _tags_overlap(m.get("tags", []), tags))
    ]
    removed = before - len(data["memories"])
    if removed:
        save_memories(data)
        logger.info(f"Cleared {removed} follow-up memories matching tags {tags}")


def _tags_overlap(tags_a: list, tags_b: list) -> bool:
    """Check if two tag lists share any person: tags or 2+ general tags."""
    set_a = set(tags_a)
    set_b = set(tags_b)
    # Any shared person tag is a strong match
    person_overlap = {t for t in set_a & set_b if t.startswith("person:")}
    if person_overlap:
        return True
    # 2+ shared general tags is a weaker match
    general_overlap = set_a & set_b - {t for t in set_a | set_b if t.startswith("person:")}
    return len(general_overlap) >= 2


def add_memories(new_memories: list[dict]):
    """Append new memories, assigning IDs and expiry dates.

    Dedup: exact content match, plus tag-based dedup (same type + overlapping
    tags → replace older memory instead of adding duplicate).

    When adding a 'resolved' memory, removes conflicting 'pending' memories
    about the same topic (matched by overlapping tags).

    Enforces per-type hard caps — oldest memories pruned when exceeded.
    """
    data = load_memories()
    existing_contents = {m["content"] for m in data["memories"]}
    now = datetime.now(timezone.utc)

    for mem in new_memories:
        if mem["content"] in existing_contents:
            continue
        mem_type = mem.get("type", "fact")
        mem_tags = mem.get("tags", [])
        expiry_days = EXPIRY_DAYS.get(mem_type)

        # When adding a resolved memory, remove conflicting pending memories
        if mem_type == "resolved" and mem_tags:
            before_count = len(data["memories"])
            data["memories"] = [
                m for m in data["memories"]
                if not (m["type"] == "pending" and _tags_overlap(m.get("tags", []), mem_tags))
            ]
            removed = before_count - len(data["memories"])
            if removed:
                logger.info(f"Resolved memory superseded {removed} pending memories (tags: {mem_tags})")

        # Tag-based dedup: if an existing memory of the same type has
        # overlapping tags, replace it instead of adding a near-duplicate
        if mem_tags:
            replaced = False
            for i, existing in enumerate(data["memories"]):
                if existing["type"] == mem_type and _tags_overlap(existing.get("tags", []), mem_tags):
                    # Replace older with newer
                    data["memories"][i] = {
                        "id": existing["id"],  # keep same ID
                        "type": mem_type,
                        "content": mem["content"],
                        "source": mem.get("source", "bot"),
                        "created_at": now.isoformat(),
                        "expires_at": (now + timedelta(days=expiry_days)).isoformat() if expiry_days else None,
                        "tags": list(dict.fromkeys(existing.get("tags", []) + mem_tags)),  # merge tags
                    }
                    replaced = True
                    logger.info(f"Tag-dedup: replaced [{mem_type}] (tags: {mem_tags})")
                    break
            if replaced:
                existing_contents.add(mem["content"])
                continue

        data["memories"].append({
            "id": str(uuid.uuid4()),
            "type": mem_type,
            "content": mem["content"],
            "source": mem.get("source", "bot"),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=expiry_days)).isoformat() if expiry_days else None,
            "tags": mem_tags,
        })
        existing_contents.add(mem["content"])

    # Enforce per-type hard caps — prune oldest when exceeded
    _enforce_type_caps(data)

    save_memories(data)


def _enforce_type_caps(data: dict):
    """Prune oldest memories when a type exceeds its hard cap."""
    from collections import defaultdict
    by_type = defaultdict(list)
    for i, m in enumerate(data["memories"]):
        by_type[m["type"]].append((i, m))

    to_remove = set()
    for mem_type, cap in TYPE_CAPS.items():
        entries = by_type.get(mem_type, [])
        if len(entries) <= cap:
            continue
        # Sort by created_at ascending, prune oldest
        entries.sort(key=lambda x: x[1].get("created_at", ""))
        excess = len(entries) - cap
        for idx, _ in entries[:excess]:
            to_remove.add(idx)
        logger.info(f"Hard cap: pruning {excess} oldest [{mem_type}] memories (cap={cap})")

    if to_remove:
        data["memories"] = [m for i, m in enumerate(data["memories"]) if i not in to_remove]


def get_preference_memories() -> list[dict]:
    """Return active preference-type memories."""
    return [m for m in get_active_memories() if m["type"] == "preference"]


def migrate_rules_to_memories():
    """Migrate rules from preferences.json into preference memories.

    Idempotent — safe to run multiple times. Clears the rules list after migration.
    """
    try:
        from preferences import load_preferences, save_preferences
        prefs = load_preferences()
        rules = prefs.get("rules", [])
        if not rules:
            return

        new_memories = []
        for rule in rules:
            # Best-effort tagging from rule text
            tags = []
            rule_lower = rule.lower()
            for keyword in ("email", "meeting", "calendar", "deadline", "travel"):
                if keyword in rule_lower:
                    tags.append(keyword)
            new_memories.append({
                "type": "preference",
                "content": rule,
                "tags": tags,
                "source": "migrated_rule",
            })

        if new_memories:
            add_memories(new_memories)
            logger.info(f"Migrated {len(new_memories)} rules to preference memories")

        # Clear rules list in preferences.json
        prefs["rules"] = []
        save_preferences(prefs)
        logger.info("Cleared rules list from preferences.json after migration")
    except Exception as e:
        logger.warning(f"Rule migration failed (non-fatal): {e}")


def get_active_memories() -> list[dict]:
    """Return non-expired individual memories, pruning expired ones."""
    data = load_memories()
    now = datetime.now(timezone.utc)
    active = []
    pruned = False

    for mem in data["memories"]:
        expires = mem.get("expires_at")
        if expires and datetime.fromisoformat(expires) < now:
            pruned = True
            continue
        active.append(mem)

    if pruned:
        data["memories"] = active
        save_memories(data)

    return active


# ---------------------------------------------------------------------------
# Prompt injection — tiered budget allocation
# ---------------------------------------------------------------------------

def get_memories_for_prompt(max_chars: int = 2000) -> str:
    """Format memories for injection into prompts with tiered priority."""
    data = load_memories()
    now = datetime.now(timezone.utc)

    # Get active (non-expired) individual memories
    active = [
        m for m in data["memories"]
        if not m.get("expires_at") or datetime.fromisoformat(m["expires_at"]) > now
    ]

    if not active and not any(data["summaries"].values()):
        return ""

    # Partition individual memories
    follow_ups = [m for m in active if m["type"] == "follow_up"]
    pending = [m for m in active if m["type"] == "pending"]
    relationships = [m for m in active if m["type"] == "relationship"]
    preferences = [m for m in active if m["type"] == "preference"]

    seven_days_ago = (now - timedelta(days=7)).isoformat()
    three_days_ago = (now - timedelta(days=3)).isoformat()
    recent_facts = [m for m in active if m["type"] == "fact" and m["created_at"] >= seven_days_ago]
    recent_resolved = [m for m in active if m["type"] == "resolved" and m["created_at"] >= three_days_ago]
    conversation_summaries = [m for m in active if m["type"] == "conversation_summary"]

    sections = []
    used = 0

    # Tier 1 (~40%): follow_ups, pending, relationships, preferences — must-include
    tier1_budget = int(max_chars * 0.4)
    tier1_lines = []
    for m in follow_ups + pending + relationships + preferences:
        line = f"- [{m['type']}] {m['content']}"
        if used + len(line) + 1 <= tier1_budget:
            tier1_lines.append(line)
            used += len(line) + 1
    if tier1_lines:
        sections.append("\n".join(tier1_lines))

    # Tier 2 (~30%): recent facts and resolved items
    tier2_limit = used + int(max_chars * 0.3)
    tier2_lines = []
    for m in sorted(recent_facts + recent_resolved + conversation_summaries, key=lambda x: x["created_at"], reverse=True):
        line = f"- [{m['type']}] {m['content']}"
        if used + len(line) + 1 <= tier2_limit:
            tier2_lines.append(line)
            used += len(line) + 1
    if tier2_lines:
        sections.append("\n".join(tier2_lines))

    # Tier 3 (remaining): historical summaries
    summaries = data.get("summaries", {})
    summary_lines = []
    for weekly in sorted(summaries.get("weekly", []), key=lambda s: s["period"], reverse=True)[:3]:
        line = f"- [week {weekly['period']}] {weekly['content']}"
        if used + len(line) + 1 <= max_chars:
            summary_lines.append(line)
            used += len(line) + 1
    for monthly in sorted(summaries.get("monthly", []), key=lambda s: s["period"], reverse=True)[:1]:
        line = f"- [month {monthly['period']}] {monthly['content']}"
        if used + len(line) + 1 <= max_chars:
            summary_lines.append(line)
            used += len(line) + 1
    for yearly in sorted(summaries.get("yearly", []), key=lambda s: s["period"], reverse=True)[:1]:
        line = f"- [year {yearly['period']}] {yearly['content']}"
        if used + len(line) + 1 <= max_chars:
            summary_lines.append(line)
            used += len(line) + 1
    if summary_lines:
        sections.append("Historical context:\n" + "\n".join(summary_lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Memory extraction from conversations
# ---------------------------------------------------------------------------

def _get_handled_context() -> str:
    """Build a list of resolved/dismissed items to prevent re-creating pending memories."""
    data = load_memories()
    resolved = [m for m in data["memories"] if m["type"] == "resolved"]

    lines = []
    for m in resolved:
        lines.append(f"- {m['content']}")

    # Also include dismissed threads from preferences
    try:
        from preferences import get_dismissed_context
        dismissed = get_dismissed_context()
        if dismissed:
            lines.append(dismissed)
    except Exception:
        pass

    if not lines:
        return "No previously handled items."
    return "\n".join(lines)


def extract_memories(conversation_text: str, source: str = "bot") -> list[dict]:
    """Use Claude to extract memories from a conversation. Returns parsed list."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        handled = _get_handled_context()
        prompt = EXTRACTION_PROMPT.format(
            source=source,
            conversation_text=conversation_text,
            already_handled=handled,
        )
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        memories = json.loads(raw)
        if not isinstance(memories, list):
            return []
        for mem in memories:
            mem["source"] = source
        logger.info(f"Extracted {len(memories)} memories from {source}")
        return memories
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Memory extraction failed: {e}")
        return []


def extract_and_store(conversation_text: str, source: str = "bot"):
    """Extract memories from text and store them. Non-fatal on failure."""
    try:
        if len(conversation_text) < 50:
            return
        memories = extract_memories(conversation_text, source)
        if memories:
            add_memories(memories)
    except Exception as e:
        logger.warning(f"Memory extract_and_store failed (non-fatal): {e}")


CONVERSATION_SUMMARY_PROMPT = """Condense this bot conversation into a 2-3 sentence summary capturing:
- What Erez asked about or wanted to do
- Key decisions made or actions taken (e.g., dismissals, searches, follow-ups)
- Any context that would help continue this thread in a future conversation

Be specific — use names, topics, and outcomes. Skip pleasantries.
Return ONLY the summary text, no JSON or formatting.

<conversation>
{conversation_log}
</conversation>"""


def summarize_conversation(conversation_log: str):
    """Generate a condensed summary of a bot conversation and store it.

    Called after multi-exchange conversations to preserve context
    across sessions. Summaries expire after 14 days and get absorbed
    into weekly compaction.
    """
    try:
        if len(conversation_log) < 200:
            return  # too short to summarize

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": CONVERSATION_SUMMARY_PROMPT.format(
                    conversation_log=conversation_log,
                ),
            }],
        )
        summary = response.content[0].text.strip()
        if summary:
            add_memories([{
                "type": "conversation_summary",
                "content": summary,
                "tags": [],
                "source": "bot_summary",
            }])
            logger.info(f"Stored conversation summary: {summary[:80]}...")
    except Exception as e:
        logger.warning(f"Conversation summary failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Hierarchical compaction
# ---------------------------------------------------------------------------

def _iso_week(dt: datetime) -> str:
    """Return ISO week string like '2026-W10'."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"



def _summarize_with_claude(items_text: str, level: str, period: str) -> str:
    """Ask Claude to summarize a batch of memories into a compact summary.
    Returns plain text summary (1-3 sentences)."""
    prompts = {
        "weekly": f"Summarize these events from {period} into 1-2 brief sentences capturing what was important. Return ONLY the summary text.\n\n{items_text}",
        "monthly": f"Summarize these weekly summaries from {period} into 1-2 sentences capturing the key themes. Return ONLY the summary text.\n\n{items_text}",
        "yearly": f"Summarize these monthly summaries from {period} into 2-3 sentences capturing the major themes and events. Return ONLY the summary text.\n\n{items_text}",
    }
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompts[level]}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return raw


def _compact_to_weekly(data: dict, now: datetime, max_periods: int = 5):
    """Summarize individual memories older than 7 days into weekly summaries."""
    cutoff = now - timedelta(days=7)
    existing_periods = {s["period"] for s in data["summaries"]["weekly"]}

    # Find compactable memories older than cutoff
    old_memories = [
        m for m in data["memories"]
        if m["type"] in COMPACTABLE_TYPES
        and datetime.fromisoformat(m["created_at"]) < cutoff
    ]

    if not old_memories:
        return

    # Group by ISO week
    by_week: dict[str, list] = {}
    for m in old_memories:
        week = _iso_week(datetime.fromisoformat(m["created_at"]))
        by_week.setdefault(week, []).append(m)

    periods_processed = 0
    for week, memories in sorted(by_week.items()):
        if week in existing_periods:
            continue
        if len(memories) < 3:
            continue
        if periods_processed >= max_periods:
            break

        items_text = "\n".join(f"- {m['content']}" for m in memories)
        try:
            summary_text = _summarize_with_claude(items_text, "weekly", week)
            data["summaries"]["weekly"].append({
                "id": str(uuid.uuid4()),
                "period": week,
                "content": summary_text,
                "source_count": len(memories),
                "created_at": now.isoformat(),
            })
            # Remove consumed individual memories
            consumed_ids = {m["id"] for m in memories}
            data["memories"] = [m for m in data["memories"] if m["id"] not in consumed_ids]
            periods_processed += 1
            logger.info(f"Weekly compaction: {week} — {len(memories)} memories → summary")
        except Exception as e:
            logger.warning(f"Weekly compaction failed for {week}: {e}")


def _compact_to_monthly(data: dict, now: datetime, max_periods: int = 3):
    """Summarize weekly summaries older than 30 days into monthly summaries."""
    cutoff = now - timedelta(days=30)
    existing_periods = {s["period"] for s in data["summaries"]["monthly"]}

    old_weeklies = [
        s for s in data["summaries"]["weekly"]
        if datetime.fromisoformat(s["created_at"]) < cutoff
    ]

    if not old_weeklies:
        return

    # Group by month — derive month from the period string (e.g., "2026-W10" → look up actual date)
    by_month: dict[str, list] = {}
    for s in old_weeklies:
        # Parse ISO week to get the month
        parts = s["period"].split("-W")
        year, week = int(parts[0]), int(parts[1])
        dt = datetime.fromisocalendar(year, week, 1)
        month = f"{dt.year}-{dt.month:02d}"
        by_month.setdefault(month, []).append(s)

    periods_processed = 0
    for month, weeklies in sorted(by_month.items()):
        if month in existing_periods:
            continue
        if len(weeklies) < 2:
            continue
        if periods_processed >= max_periods:
            break

        items_text = "\n".join(f"- {s['content']}" for s in weeklies)
        try:
            summary_text = _summarize_with_claude(items_text, "monthly", month)
            data["summaries"]["monthly"].append({
                "id": str(uuid.uuid4()),
                "period": month,
                "content": summary_text,
                "source_count": len(weeklies),
                "created_at": now.isoformat(),
            })
            consumed_ids = {s["id"] for s in weeklies}
            data["summaries"]["weekly"] = [s for s in data["summaries"]["weekly"] if s["id"] not in consumed_ids]
            periods_processed += 1
            logger.info(f"Monthly compaction: {month} — {len(weeklies)} weekly summaries → summary")
        except Exception as e:
            logger.warning(f"Monthly compaction failed for {month}: {e}")


def _compact_to_yearly(data: dict, now: datetime):
    """Summarize monthly summaries from completed years into yearly summaries."""
    current_year = str(now.year)
    existing_periods = {s["period"] for s in data["summaries"]["yearly"]}

    # Only compact completed years
    old_monthlies = [
        s for s in data["summaries"]["monthly"]
        if s["period"][:4] < current_year
    ]

    if not old_monthlies:
        return

    by_year: dict[str, list] = {}
    for s in old_monthlies:
        year = s["period"][:4]
        by_year.setdefault(year, []).append(s)

    for year, monthlies in sorted(by_year.items()):
        if year in existing_periods:
            continue
        if len(monthlies) < 3:
            continue

        items_text = "\n".join(f"- {s['content']}" for s in monthlies)
        try:
            summary_text = _summarize_with_claude(items_text, "yearly", year)
            data["summaries"]["yearly"].append({
                "id": str(uuid.uuid4()),
                "period": year,
                "content": summary_text,
                "source_count": len(monthlies),
                "created_at": now.isoformat(),
            })
            consumed_ids = {s["id"] for s in monthlies}
            data["summaries"]["monthly"] = [s for s in data["summaries"]["monthly"] if s["id"] not in consumed_ids]
            logger.info(f"Yearly compaction: {year} — {len(monthlies)} monthly summaries → summary")
        except Exception as e:
            logger.warning(f"Yearly compaction failed for {year}: {e}")


def compact_memories():
    """Hierarchical memory compaction: individual → weekly → monthly → yearly."""
    data = load_memories()
    now = datetime.now(timezone.utc)

    # Stage 1: individual → weekly (resolved/fact memories older than 7 days)
    _compact_to_weekly(data, now)

    # Stage 2: weekly → monthly (weekly summaries older than 30 days)
    _compact_to_monthly(data, now)

    # Stage 3: monthly → yearly (completed years only)
    _compact_to_yearly(data, now)

    # Prune expired individual memories
    data["memories"] = [
        m for m in data["memories"]
        if not m.get("expires_at") or datetime.fromisoformat(m["expires_at"]) > now
    ]

    data["last_compaction"] = now.isoformat()
    save_memories(data)


# ---------------------------------------------------------------------------
# Monthly memory review
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """You are a memory auditor for a personal assistant called Claudette.
Review the following memory store and identify issues that need the user's input.

Look for:
1. CONTRADICTIONS — a "pending" item and a "resolved" item about the same thing
2. STALE ITEMS — "pending" items that are very old and may have been silently handled
3. AMBIGUITIES — memories that are vague or could mean multiple things
4. DUPLICATES — multiple memories saying essentially the same thing
5. STALE PREFERENCES — "preference" items that may no longer apply (e.g., a preference about a sender who hasn't emailed in months, or preferences that contradict each other). Suggest removing or updating them.
6. STALE FACTS — "fact" items that are old and likely no longer true (e.g., "Erez is preparing for next week's talk" from a month ago). Suggest removing.

Current memories:
{memories_text}

Format your response as a friendly, concise Telegram message to Erez asking him to clarify the issues you found. Group by issue type. For each item, quote the memory and suggest a resolution (e.g., "Is this still pending or can I mark it resolved?", "Is this preference still accurate?"). If everything looks clean, just say so briefly.

Keep it warm and short — this is Telegram, not a report."""


def generate_memory_review() -> str | None:
    """Generate a memory review message for the user. Returns None if memory is clean."""
    data = load_memories()
    memories = data["memories"]

    if len(memories) < 5:
        return None

    memories_text = "\n".join(
        f"- [{m['type']}] {m['content']} (created {m['created_at'][:10]}, tags: {m.get('tags', [])})"
        for m in memories
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": REVIEW_PROMPT.format(memories_text=memories_text)}],
        )
        review = response.content[0].text.strip()

        # If Claude says everything is clean, skip sending
        clean_signals = ["everything looks clean", "no issues", "all clear", "looks good", "nothing to flag"]
        if any(s in review.lower() for s in clean_signals):
            logger.info("Memory review: all clean, nothing to send")
            return None

        logger.info("Memory review: found issues to raise with user")
        return review
    except Exception as e:
        logger.warning(f"Memory review generation failed: {e}")
        return None


def should_run_review() -> bool:
    """Check if a memory review is due (at most once per week)."""
    data = load_memories()
    last_review = data.get("last_review")
    now = datetime.now(timezone.utc)

    if last_review:
        days_since = (now - datetime.fromisoformat(last_review)).days
        if days_since < 6:
            return False

    return True


def mark_review_done():
    """Record that a monthly review has been completed."""
    data = load_memories()
    data["last_review"] = datetime.now(timezone.utc).isoformat()
    save_memories(data)
