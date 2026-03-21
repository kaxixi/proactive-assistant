"""Entry point for scheduled daily runs and manual triggers."""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from calendar_digest import get_upcoming_meetings, get_meetings_for_range, get_user_timezone, Meeting
from analyzer import generate_daily_digest
import anthropic

from preferences import load_preferences, get_dismissed_context, get_dismissed_thread_ids
from priorities import fetch_priorities
from memory import (
    get_memories_for_prompt, extract_and_store, compact_memories,
    generate_memory_review, mark_review_done,
    get_active_memories, migrate_rules_to_memories,
)
from bot import send_message
from config import DIGEST_HOUR, DIGEST_MINUTE, ENABLE_EMAIL, ANTHROPIC_API_KEY, CLAUDE_MODEL
from open_loops import (
    OpenLoop, get_open_loops, get_loop_thread_ids, upsert_loops,
)

if ENABLE_EMAIL:
    from email_monitor import scan_inbox, FlaggedEmail
else:
    FlaggedEmail = None  # type reference only

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Maximum loops to pass to Claude after pre-processing
MAX_LOOPS = 15


def _get_digest_type_and_calendar(local_now: datetime) -> tuple[str, list]:
    """Determine digest type and fetch appropriate calendar range."""
    weekday = local_now.weekday()  # 0=Mon, 5=Sat, 6=Sun

    if weekday == 5:  # Saturday
        # Weekend digest: fetch Sat + Sun
        meetings = get_meetings_for_range(days=2)
        return "weekend", meetings
    elif weekday == 6:  # Sunday
        # Week-ahead digest: fetch Sun + next Mon-Fri (7 days)
        meetings = get_meetings_for_range(days=7)
        return "week_ahead", meetings
    else:  # Mon-Fri
        meetings = get_upcoming_meetings()  # today + tomorrow
        return "weekday", meetings


# ---------------------------------------------------------------------------
# Pre-processing pipeline
# ---------------------------------------------------------------------------

def _hard_filter_dismissed(emails: list[FlaggedEmail]) -> list[FlaggedEmail]:
    """Remove emails whose thread_id matches a dismissed thread or dismissed loop."""
    dismissed_ids = get_dismissed_thread_ids()
    # Also include thread IDs from dismissed loops
    loop_dismissed_ids = get_loop_thread_ids(status="dismissed")
    all_dismissed = dismissed_ids | loop_dismissed_ids
    if not all_dismissed:
        return emails
    before = len(emails)
    filtered = [e for e in emails if e.thread_id not in all_dismissed]
    removed = before - len(filtered)
    if removed:
        logger.info(f"Pre-processing: hard-filtered {removed} dismissed thread(s) (legacy: {len(dismissed_ids)}, loops: {len(loop_dismissed_ids)})")
    return filtered



def _extract_tagged_memories(mem_type: str) -> list[dict]:
    """Extract active memories of given type that have person: or topic: tags.

    Returns list of dicts with keys: content, tag_type ("person" or "topic"),
    value (lowercase), created_at.  A single memory may produce multiple entries
    if it has both person and topic tags.
    """
    active = get_active_memories()
    results = []
    for m in active:
        if m["type"] != mem_type:
            continue
        for tag in m.get("tags", []):
            for prefix in ("person:", "topic:"):
                if tag.startswith(prefix):
                    value = tag[len(prefix):].strip()
                    if value:
                        results.append({
                            "content": m["content"],
                            "tag_type": prefix.rstrip(":"),
                            "value": value.lower(),
                            "created_at": m["created_at"],
                        })
    return results


def _substring_match(needle: str, haystack: str) -> bool:
    """Substring match with short-string protection.

    Needles under 4 chars require exact word-boundary match to avoid false
    positives (e.g. "Li" matching "Application").
    """
    if len(needle) < 4:
        tokens = re.findall(r'\b\w+\b', haystack)
        return needle in tokens
    return needle in haystack



def _tag_matches_text(value: str, text: str) -> bool:
    """Check if a tagged memory value appears in a formatted text line."""
    return _substring_match(value, text.lower())




def _group_into_loops(emails: list[FlaggedEmail]) -> list[OpenLoop]:
    """Call Claude to group flagged emails into topic-level open loops.

    Merges with existing loops from open_loops.json so new emails can
    join existing loops.
    """
    if not emails:
        return get_open_loops()

    existing_loops = get_open_loops()

    # Build the prompt
    email_lines = []
    for e in emails:
        email_lines.append(
            f"- thread_id: {e.thread_id}, subject: {e.subject}, "
            f"sender: {e.sender_name} <{e.sender}>, age_days: {e.age_days}, "
            f"urgency: {e.urgency}, reason: {e.reason}, snippet: {e.snippet}"
        )

    existing_loop_lines = []
    for l in existing_loops:
        existing_loop_lines.append(
            f"- loop_id: {l.loop_id}, title: {l.title}, "
            f"thread_ids: {l.thread_ids}, senders: {l.senders}, "
            f"tags: {l.tags}"
        )

    # Gather learned context: resolved memories, preferences, and follow-ups
    resolved = _extract_tagged_memories("resolved")
    follow_ups = _extract_tagged_memories("follow_up")
    from memory import get_preference_memories
    pref_memories = get_preference_memories()

    context_lines = []
    if resolved:
        context_lines.append("Recently resolved (deprioritize related emails):")
        for r in resolved:
            context_lines.append(f"  - [{r['tag_type']}:{r['value']}] {r['content']}")
    if follow_ups:
        context_lines.append("Active follow-ups (flag these as higher priority):")
        for fu in follow_ups:
            context_lines.append(f"  - [{fu['tag_type']}:{fu['value']}] {fu['content']}")
    if pref_memories:
        context_lines.append("Learned preferences:")
        for pm in pref_memories:
            context_lines.append(f"  - {pm['content']}")

    memory_block = "\n".join(context_lines) if context_lines else "None"

    prompt = f"""You are grouping emails into topic-level "open loops". An open loop is a topic or concern that may span multiple email threads (e.g., "Arjun's HCRP application" groups emails from both Arjun and CommunityForce about the same application).

<emails>
{chr(10).join(email_lines)}
</emails>

<existing_loops>
{chr(10).join(existing_loop_lines) if existing_loop_lines else "None"}
</existing_loops>

<learned_context>
{memory_block}
</learned_context>

Instructions:
1. Assign each email to an existing loop OR create a new loop.
2. Use judgment: emails about the same topic from different senders belong in the same loop.
3. Each loop needs a short, descriptive title and 1-2 sentence summary.
4. Generate person: and topic: tags for each loop.
5. Keep loop titles stable — if an email fits an existing loop, use that loop's ID.
6. Use the learned context to set urgency:
   - Emails matching resolved items → set urgency to "low"
   - Emails matching active follow-ups → set urgency to "high"
   - Apply learned preferences (e.g., "skip recruiting emails" → urgency "low")

Return ONLY valid JSON (no markdown fences) with this structure:
{{
  "loops": [
    {{
      "loop_id": "existing_id_or_NEW",
      "title": "Short descriptive title",
      "summary": "1-2 sentence summary of what this loop is about",
      "thread_ids": ["id1", "id2"],
      "senders": ["email1@example.com"],
      "tags": ["person:Arjun", "topic:hcrp"],
      "urgency": "high/medium/low"
    }}
  ]
}}

For new loops, set loop_id to "NEW". For existing loops, use their existing loop_id."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        # Fix common JSON issues: trailing commas before ] or }
        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        # If response was truncated, try to salvage by closing open structures
        if not raw.endswith('}'):
            # Find last complete loop entry
            last_brace = raw.rfind('}')
            if last_brace > 0:
                raw = raw[:last_brace + 1] + ']}'
        result = json.loads(raw)
    except Exception as e:
        logger.error(f"Loop grouping Claude call failed: {e}")
        # Fallback: one loop per email
        return _fallback_loops(emails)

    # Build email lookup for derived fields
    email_by_thread = {e.thread_id: e for e in emails}
    urgency_order = {"high": 0, "medium": 1, "low": 2}

    from open_loops import _new_id

    new_loops = []
    for loop_data in result.get("loops", []):
        loop_id = loop_data.get("loop_id", "NEW")
        if loop_id == "NEW":
            loop_id = _new_id()

        thread_ids = loop_data.get("thread_ids", [])
        senders = loop_data.get("senders", [])

        # Compute derived fields from member emails
        member_emails = [email_by_thread[tid] for tid in thread_ids if tid in email_by_thread]
        if member_emails:
            email_urgency = min(member_emails, key=lambda e: urgency_order.get(e.urgency, 2)).urgency
            max_age = max(e.age_days for e in member_emails)
            reasons = set(e.reason for e in member_emails)
            reason = "mixed" if len(reasons) > 1 else reasons.pop()
            snippets = [e.snippet for e in member_emails[:3]]
        else:
            email_urgency = "low"
            max_age = 0
            reason = "unreplied"
            snippets = []

        # Use Claude's urgency if provided (informed by learned context),
        # otherwise fall back to max email urgency
        claude_urgency = loop_data.get("urgency")
        loop_urgency = claude_urgency if claude_urgency in urgency_order else email_urgency

        new_loops.append(OpenLoop(
            loop_id=loop_id,
            title=loop_data.get("title", "Untitled"),
            summary=loop_data.get("summary", ""),
            thread_ids=thread_ids,
            senders=senders,
            urgency=loop_urgency,
            age_days=max_age,
            reason=reason,
            snippets=snippets,
            tags=loop_data.get("tags", []),
        ))

    upsert_loops(new_loops)
    logger.info(f"Grouped {len(emails)} emails into {len(new_loops)} loops")
    return get_open_loops()


def _fallback_loops(emails: list[FlaggedEmail]) -> list[OpenLoop]:
    """Fallback: create one loop per email if Claude grouping fails."""
    from open_loops import _new_id
    loops = []
    for e in emails:
        loops.append(OpenLoop(
            loop_id=_new_id(),
            title=e.subject,
            summary=f"From {e.sender_name}: {e.snippet[:100]}",
            thread_ids=[e.thread_id],
            senders=[e.sender],
            urgency=e.urgency,
            age_days=e.age_days,
            reason=e.reason,
            snippets=[e.snippet],
            tags=[f"person:{e.sender_name}"],
        ))
    upsert_loops(loops)
    return get_open_loops()


def _priority_match_loops(loops: list[OpenLoop], priorities: str) -> list[OpenLoop]:
    """Tag loops that match keywords from the priorities list."""
    if not priorities:
        return loops

    priority_lines = [
        line.strip() for line in priorities.splitlines()
        if line.strip() and len(line.strip()) > 3
    ]

    keywords = []
    for line in priority_lines:
        keywords.append(line.lower())
        for word in re.findall(r'\b[a-zA-Z]{4,}\b', line):
            if word.lower() not in {
                "with", "from", "that", "this", "have", "will", "been",
                "more", "about", "would", "their", "there", "which", "could",
                "should", "other", "than", "into", "some", "what", "your",
                "when", "them", "then", "each", "make", "like", "just",
                "over", "also", "back", "after", "work", "only", "most",
                "very", "here", "need", "want", "does", "done",
            }:
                keywords.append(word.lower())

    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)

    matched_count = 0
    for loop in loops:
        search_text = f"{loop.title} {' '.join(loop.senders)} {' '.join(loop.tags)}".lower()
        matches = [kw for kw in unique_keywords if kw in search_text]
        if matches:
            best_match = max(matches, key=len)
            loop.summary = f"[MATCHES PRIORITY: '{best_match}'] {loop.summary}"
            matched_count += 1

    if matched_count:
        logger.info(f"Pre-processing: matched {matched_count} loop(s) to priorities")
    return loops


def _cap_loops(loops: list[OpenLoop]) -> tuple[list[OpenLoop], str]:
    """Sort by urgency and cap at MAX_LOOPS."""
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    loops.sort(key=lambda l: (urgency_order.get(l.urgency, 2), -l.age_days))

    overflow_note = ""
    if len(loops) > MAX_LOOPS:
        overflow_note = f"Plus {len(loops) - MAX_LOOPS} lower-priority loops not shown."
        loops = loops[:MAX_LOOPS]
        logger.info(f"Pre-processing: capped loops to {MAX_LOOPS}, {overflow_note}")

    return loops, overflow_note


def _group_loops_by_priority(loops: list[OpenLoop]) -> str:
    """Format loops into high/medium/low sections as text."""
    groups = {"high": [], "medium": [], "low": []}
    for l in loops:
        groups.get(l.urgency, groups["low"]).append(l)

    lines = []
    for level, label in [("high", "HIGH PRIORITY"), ("medium", "MEDIUM PRIORITY"), ("low", "LOW PRIORITY")]:
        group = groups[level]
        if not group:
            continue
        lines.append(f"--- {label} ({len(group)} loop{'s' if len(group) != 1 else ''}) ---")
        for l in group:
            sender_list = ", ".join(l.senders[:3])
            lines.append(
                f"- [loop:{l.loop_id}] \"{l.title}\" — {len(l.thread_ids)} thread(s) "
                f"({sender_list}), oldest {l.age_days} days, reason: {l.reason}"
            )
            if l.summary:
                lines.append(f"  Summary: {l.summary}")
            if l.snippets:
                lines.append(f"  Latest: {l.snippets[0][:200]}")
            lines.append(f"  Tags: {', '.join(l.tags)}")
        lines.append("")

    return "\n".join(lines) if lines else "None — inbox looks clean."


def _apply_follow_up_to_loops(loops: list[OpenLoop], loops_xml: str) -> str:
    """Annotate loops with follow-up reminders and inject synthetic lines for non-loop follow-ups."""
    follow_ups = _extract_tagged_memories("follow_up")
    if not follow_ups:
        return loops_xml

    matched = set()
    unmatched = []

    for fu in follow_ups:
        found = False
        for loop in loops:
            # Check if follow-up matches any loop by tags or title
            loop_text = f"{loop.title} {' '.join(loop.senders)} {' '.join(loop.tags)}".lower()
            if _tag_matches_text(fu["value"], loop_text):
                found = True
                matched.add((fu["tag_type"], fu["value"], fu["content"]))
                break
        if not found:
            unmatched.append(fu)

    updated_xml = loops_xml
    for tag_type, value, content in matched:
        lines = updated_xml.split("\n")
        for i, line in enumerate(lines):
            if _tag_matches_text(value, line):
                if "[FOLLOW-UP REMINDER:" not in line:
                    lines[i] = line + f" [FOLLOW-UP REMINDER: {content}]"
                    break
        updated_xml = "\n".join(lines)

    if unmatched:
        synthetic_lines = ["\n--- FOLLOW-UP REMINDERS (no email thread) ---"]
        for fu in unmatched:
            synthetic_lines.append(
                f"- [FOLLOW-UP — no email thread] {fu['value'].title()}: {fu['content']}"
            )
        updated_xml += "\n".join(synthetic_lines)

    annotated = len(matched)
    injected = len(unmatched)
    if annotated or injected:
        logger.info(
            f"Pre-processing: follow-up logic (loops) — {annotated} annotated, {injected} synthetic injected"
        )

    return updated_xml



def _format_calendar(meetings: list[Meeting], local_now: datetime) -> str:
    """Pre-format calendar with correct day labels using user's timezone."""
    if not meetings:
        return "No meetings scheduled."

    today = local_now.date()
    tomorrow = today + timedelta(days=1)

    lines = []
    current_day_label = None

    for m in meetings:
        meeting_date = m.start.date()

        # Compute day label — include day name so Claude doesn't guess wrong
        day_name = m.start.strftime("%A")
        if meeting_date == today:
            day_label = f"TODAY ({day_name})"
        elif meeting_date == tomorrow:
            day_label = f"TOMORROW ({day_name})"
        else:
            day_label = day_name.upper()

        # Add day header if it changed
        if day_label != current_day_label:
            if current_day_label is not None:
                lines.append("")  # blank line between days
            date_str = m.start.strftime("%B %d")  # e.g., "March 17"
            lines.append(f"[{day_label} — {date_str}]")
            current_day_label = day_label

        # Format time — skip all-day events that are just reminders (birthdays, pet care)
        if "T" not in m.start.isoformat():
            time_str = "ALL-DAY EVENT"
        else:
            time_str = m.start.strftime("%I:%M %p").lstrip("0")

        # Build meeting line
        recurrence = "ONE-TIME" if not m.is_recurring else "recurring"
        prep_flag = f" | NEEDS PREP: {m.prep_reason}" if m.needs_prep else ""
        location = f" | Location: {m.location}" if m.location else ""
        attendees = f" | {len(m.attendees)} attendees" if m.attendees else ""

        lines.append(
            f"- {time_str}: {m.summary} ({recurrence}){attendees}{location}{prep_flag}"
        )

    return "\n".join(lines)


def _format_preferences(prefs: dict) -> str:
    """Format preferences as text for XML tag, reading from preference memories."""
    from memory import get_preference_memories
    pref_memories = get_preference_memories()
    if not pref_memories:
        return ""
    return "Learned preferences:\n" + "\n".join(f"- {m['content']}" for m in pref_memories)


def preprocess_for_digest(
    flagged_emails: list[FlaggedEmail],
    meetings: list[Meeting],
    prefs: dict,
    priorities: str,
    memories_context: str,
    dismissed_context: str,
    local_now: datetime,
) -> dict:
    """Run the full pre-processing pipeline and return formatted strings
    ready to be dropped into analyzer.py XML tags.

    When email is enabled, groups emails into open loops (topic-level tracking).

    Returns dict with keys: emails_xml, calendar_xml, priorities_xml,
    preferences_xml, memories_xml, dismissed_xml, overflow_note
    """
    logger.info(f"Pre-processing: starting with {len(flagged_emails)} emails, {len(meetings)} meetings")

    if ENABLE_EMAIL and flagged_emails:
        # (a) Hard filter dismissed thread IDs (legacy + loop-dismissed)
        emails = _hard_filter_dismissed(flagged_emails)

        # (b) Group into open loops via Claude
        logger.info("Grouping emails into open loops...")
        loops = _group_into_loops(emails)

        # (c) Priority matching on loops
        loops = _priority_match_loops(loops, priorities)

        # (d) Cap loops
        loops, overflow_note = _cap_loops(loops)

        # (e) Format loops by priority
        emails_xml = _group_loops_by_priority(loops)

        # (f) Follow-up logic on loops
        emails_xml = _apply_follow_up_to_loops(loops, emails_xml)

        logger.info(f"Pre-processing complete: {len(loops)} loops, {len(meetings)} meetings")
    else:
        emails_xml = "None — inbox looks clean." if ENABLE_EMAIL else ""
        overflow_note = ""
        logger.info(f"Pre-processing complete: no emails, {len(meetings)} meetings")

    # Calendar pre-formatting with day labels
    calendar_xml = _format_calendar(meetings, local_now)

    # Format other sections
    priorities_xml = priorities if priorities else ""
    preferences_xml = _format_preferences(prefs)
    memories_xml = memories_context if memories_context else ""
    dismissed_xml = dismissed_context if dismissed_context else ""

    return {
        "emails_xml": emails_xml,
        "calendar_xml": calendar_xml,
        "priorities_xml": priorities_xml,
        "preferences_xml": preferences_xml,
        "memories_xml": memories_xml,
        "dismissed_xml": dismissed_xml,
        "overflow_note": overflow_note,
    }


# ---------------------------------------------------------------------------
# Scheduling logic
# ---------------------------------------------------------------------------

def is_digest_time() -> tuple[bool, datetime | None]:
    """Check if it's currently digest time in the user's calendar timezone.
    Returns (is_time, local_now) so caller can use the local time for day-of-week."""
    try:
        tz_name = get_user_timezone()
        now = datetime.now(ZoneInfo(tz_name))
        # Match if we're within 90 min of the target time (timer runs every 3h)
        target_minutes = DIGEST_HOUR * 60 + DIGEST_MINUTE
        current_minutes = now.hour * 60 + now.minute
        diff = abs(current_minutes - target_minutes)
        # Handle midnight wraparound
        diff = min(diff, 1440 - diff)
        is_time = diff < 90
        logger.info(
            f"Timezone: {tz_name}, local time: {now.strftime('%H:%M %A')}, "
            f"target: {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}, "
            f"diff: {diff}min, sending: {is_time}"
        )
        return is_time, now
    except Exception as e:
        logger.warning(f"Could not check timezone, proceeding anyway: {e}")
        return True, datetime.now()


async def run_daily_digest(local_now: datetime = None):
    """Run the full daily digest pipeline and send via Telegram."""
    logger.info("Starting daily digest...")

    try:
        # Determine digest type and fetch calendar
        if local_now is None:
            try:
                tz_name = get_user_timezone()
                local_now = datetime.now(ZoneInfo(tz_name))
            except Exception:
                local_now = datetime.now()

        digest_type, meetings = _get_digest_type_and_calendar(local_now)
        logger.info(f"Digest type: {digest_type} ({local_now.strftime('%A')}), {len(meetings)} meetings")

        # 1. Scan emails (if enabled)
        if ENABLE_EMAIL:
            logger.info("Scanning inbox...")
            flagged_emails = scan_inbox()
            logger.info(f"Found {len(flagged_emails)} flagged emails")
        else:
            logger.info("Email scanning disabled — calendar-only mode")
            flagged_emails = []

        # 2. Load preferences, priorities, and memories
        prefs = load_preferences()
        logger.info("Fetching priorities...")
        priorities = fetch_priorities()
        memories_context = get_memories_for_prompt()
        dismissed_context = get_dismissed_context() if ENABLE_EMAIL else ""

        # 3. Pre-process: filter, tag, group, format
        logger.info("Running pre-processing pipeline...")
        processed = preprocess_for_digest(
            flagged_emails, meetings, prefs, priorities,
            memories_context, dismissed_context, local_now,
        )

        # 4. Generate digest with Claude (pre-processed strings)
        logger.info("Generating digest with Claude...")
        digest = generate_daily_digest(
            emails_xml=processed["emails_xml"],
            calendar_xml=processed["calendar_xml"],
            priorities_xml=processed["priorities_xml"],
            preferences_xml=processed["preferences_xml"],
            memories_xml=processed["memories_xml"],
            dismissed_xml=processed["dismissed_xml"],
            digest_type=digest_type,
            overflow_note=processed["overflow_note"],
        )

        # 5. Send via Telegram
        await send_message(digest)
        logger.info("Daily digest sent successfully")

        # 6. Extract memories from the digest we just sent
        try:
            extract_and_store(f"Daily digest sent to Erez:\n{digest}", source="digest")
        except Exception as e:
            logger.warning(f"Memory extraction from digest failed (non-fatal): {e}")

        # 7. Run memory compaction if needed
        try:
            compact_memories()
        except Exception as e:
            logger.warning(f"Memory compaction failed (non-fatal): {e}")

        # 8. Sunday: memory review + housekeeping
        if digest_type == "week_ahead":
            try:
                logger.info("Running weekly memory review (Sunday)...")
                review = generate_memory_review()
                if review:
                    await send_message(f"🧠 Weekly memory check-in:\n\n{review}")
                mark_review_done()
            except Exception as e:
                logger.warning(f"Memory review failed (non-fatal): {e}")

    except FileNotFoundError as e:
        error_msg = f"⚠️ Setup incomplete: {e}"
        logger.error(error_msg)
        await send_message(error_msg)
    except Exception as e:
        error_msg = f"⚠️ Digest failed: {type(e).__name__}: {e}"
        logger.error(error_msg, exc_info=True)
        await send_message(error_msg)


def main():
    # Run migration at startup (idempotent)
    migrate_rules_to_memories()

    # When called with --force, skip the time check
    if "--force" in sys.argv:
        logger.info("Force mode — skipping time check")
        asyncio.run(run_daily_digest())
        return

    is_time, local_now = is_digest_time()
    if not is_time:
        logger.info("Not digest time — exiting")
        return

    asyncio.run(run_daily_digest(local_now))


if __name__ == "__main__":
    main()
