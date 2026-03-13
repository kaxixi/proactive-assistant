"""Memory system — extracts and stores key facts from conversations and digests."""

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
    "relationship": None,  # no expiry — relationships persist
    "fact": 60,
    "preference": None,  # no expiry — graduates to preferences.py rules
}

DEFAULT_MEMORY = {
    "memories": [],
    "last_compaction": None,
}

EXTRACTION_PROMPT = """You are a memory extraction system for a personal assistant called Claudette.
Analyze the following interaction and extract key memories worth retaining.

For each memory, return a JSON array of objects with these fields:
- "type": one of "resolved", "pending", "relationship", "fact", "preference"
- "content": a concise, self-contained statement (one sentence)
- "tags": array of relevant tags (person names as "person:Name", or general tags like "email", "meeting", "travel", "deadline")

Type guidelines:
- "resolved": something completed, handled, or no longer needs attention
- "pending": a task, follow-up, or open item Erez still needs to act on
- "relationship": information about a person and their role/relationship to Erez
- "fact": a concrete fact about Erez's schedule, plans, or situation
- "preference": a lasting preference about how Erez wants things handled

Rules:
- Only extract genuinely useful information. Skip pleasantries and small talk.
- If nothing worth remembering happened, return an empty array [].
- Each memory must be self-contained — understandable without the original context.
- Keep each "content" field to ONE sentence.

Return ONLY valid JSON. No explanation text.

Interaction ({source}):
{conversation_text}"""


def load_memories() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return DEFAULT_MEMORY.copy()


def save_memories(data: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def add_memories(new_memories: list[dict]):
    """Append new memories, assigning IDs and expiry dates."""
    data = load_memories()
    existing_contents = {m["content"] for m in data["memories"]}
    now = datetime.now(timezone.utc)

    for mem in new_memories:
        if mem["content"] in existing_contents:
            continue
        mem_type = mem.get("type", "fact")
        expiry_days = EXPIRY_DAYS.get(mem_type)
        data["memories"].append({
            "id": str(uuid.uuid4()),
            "type": mem_type,
            "content": mem["content"],
            "source": mem.get("source", "bot"),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=expiry_days)).isoformat() if expiry_days else None,
            "tags": mem.get("tags", []),
        })
        existing_contents.add(mem["content"])

    save_memories(data)


def get_active_memories(max_count: int = 50) -> list[dict]:
    """Return non-expired memories, pruning expired ones."""
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

    # Sort by recency, return most recent
    active.sort(key=lambda m: m["created_at"], reverse=True)
    return active[:max_count]


def get_memories_for_prompt(max_chars: int = 2000) -> str:
    """Format active memories as text for injection into prompts."""
    memories = get_active_memories()
    if not memories:
        return ""

    # Group by type, prioritize: pending > fact > relationship > resolved > preference
    type_order = {"pending": 0, "fact": 1, "relationship": 2, "resolved": 3, "preference": 4}
    memories.sort(key=lambda m: (type_order.get(m["type"], 5), m["created_at"]))

    lines = []
    current_len = 0
    for mem in memories:
        line = f"- [{mem['type']}] {mem['content']}"
        if current_len + len(line) > max_chars:
            break
        lines.append(line)
        current_len += len(line) + 1

    return "\n".join(lines)


def extract_memories(conversation_text: str, source: str = "bot") -> list[dict]:
    """Use Claude to extract memories from a conversation. Returns parsed list."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = EXTRACTION_PROMPT.format(
            source=source,
            conversation_text=conversation_text,
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
        # Tag with source
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
        # Skip trivially short interactions
        if len(conversation_text) < 50:
            return
        memories = extract_memories(conversation_text, source)
        if memories:
            add_memories(memories)
    except Exception as e:
        logger.warning(f"Memory extract_and_store failed (non-fatal): {e}")


def compact_memories():
    """Summarize old memories to keep the store compact. Runs if count > 100 or weekly."""
    data = load_memories()
    now = datetime.now(timezone.utc)

    last = data.get("last_compaction")
    count = len(data["memories"])
    if last:
        days_since = (now - datetime.fromisoformat(last)).days
        if count < 100 and days_since < 7:
            return

    if count < 20:
        data["last_compaction"] = now.isoformat()
        save_memories(data)
        return

    # Group resolved memories older than 7 days and summarize
    old_resolved = [
        m for m in data["memories"]
        if m["type"] == "resolved"
        and (now - datetime.fromisoformat(m["created_at"])).days > 7
    ]

    if len(old_resolved) < 5:
        data["last_compaction"] = now.isoformat()
        save_memories(data)
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        items = "\n".join(f"- {m['content']}" for m in old_resolved)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": (
                "Summarize these resolved items into 1-3 brief summary statements. "
                "Return a JSON array of objects with 'content' and 'tags' fields.\n\n"
                f"{items}"
            )}],
        )
        summaries = json.loads(response.content[0].text.strip())

        # Remove old resolved, add summaries
        old_ids = {m["id"] for m in old_resolved}
        data["memories"] = [m for m in data["memories"] if m["id"] not in old_ids]
        for s in summaries:
            data["memories"].append({
                "id": str(uuid.uuid4()),
                "type": "resolved",
                "content": s["content"],
                "source": "compaction",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(days=14)).isoformat(),
                "tags": s.get("tags", []),
            })

        data["last_compaction"] = now.isoformat()
        save_memories(data)
        logger.info(f"Compacted {len(old_resolved)} resolved memories into {len(summaries)} summaries")
    except Exception as e:
        logger.warning(f"Memory compaction failed (non-fatal): {e}")
        data["last_compaction"] = now.isoformat()
        save_memories(data)
