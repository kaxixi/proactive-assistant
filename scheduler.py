"""Entry point for scheduled daily runs and manual triggers."""

import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from email_monitor import scan_inbox, FlaggedEmail
from calendar_digest import get_upcoming_meetings, get_meetings_for_range, get_user_timezone, Meeting
from analyzer import generate_daily_digest
from preferences import load_preferences, get_dismissed_context, get_dismissed_thread_ids
from priorities import fetch_priorities
from memory import (
    get_memories_for_prompt, extract_and_store, compact_memories,
    generate_memory_review, mark_review_done,
)
from bot import send_message
from config import DIGEST_HOUR, DIGEST_MINUTE

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Maximum emails to pass to Claude after pre-processing
MAX_EMAILS = 25


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
    """Remove emails whose thread_id exactly matches a dismissed thread."""
    dismissed_ids = get_dismissed_thread_ids()
    if not dismissed_ids:
        return emails
    before = len(emails)
    filtered = [e for e in emails if e.thread_id not in dismissed_ids]
    removed = before - len(filtered)
    if removed:
        logger.info(f"Pre-processing: hard-filtered {removed} dismissed thread(s)")
    return filtered


def _get_dismissed_senders(prefs: dict) -> dict[str, str]:
    """Extract sender email → reason mapping from dismissed threads.

    We look at the sender field stored on the dismissed thread entries.
    Since dismissed threads store subject and reason but not sender email directly,
    we build a mapping from the dismissed_threads list. The dismiss_email function
    in bot.py stores the subject — we'll also check thread data if available.
    """
    dismissed = prefs.get("dismissed_threads", [])
    sender_reasons = {}
    for d in dismissed:
        # If a sender_email was stored, use it directly
        sender = d.get("sender_email", "")
        if sender:
            sender_reasons[sender.lower()] = d.get("reason", "handled")
        # Also extract from subject line patterns like "from: user@example.com"
        subject = d.get("subject", "")
        # Look for email-like patterns in the subject
        email_match = re.findall(r'[\w.+-]+@[\w.-]+\.\w+', subject)
        for em in email_match:
            sender_reasons[em.lower()] = d.get("reason", "handled")
    return sender_reasons


def _fuzzy_dismiss_tag(emails: list[FlaggedEmail], prefs: dict) -> list[FlaggedEmail]:
    """Tag emails whose sender matches a dismissed thread's sender."""
    dismissed_senders = _get_dismissed_senders(prefs)
    if not dismissed_senders:
        return emails

    tagged_count = 0
    for email in emails:
        sender_lower = email.sender.lower()
        if sender_lower in dismissed_senders:
            reason = dismissed_senders[sender_lower]
            email.snippet = f"[NOTE: sender was recently dismissed for: '{reason}'] {email.snippet}"
            tagged_count += 1

    if tagged_count:
        logger.info(f"Pre-processing: fuzzy-dismiss tagged {tagged_count} email(s)")
    return emails


def _cap_emails(emails: list[FlaggedEmail]) -> tuple[list[FlaggedEmail], str]:
    """Sort by urgency (high first) and cap at MAX_EMAILS. Return overflow note."""
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    emails.sort(key=lambda e: (urgency_order.get(e.urgency, 2), -e.age_days))

    overflow_note = ""
    if len(emails) > MAX_EMAILS:
        overflow_note = f"Plus {len(emails) - MAX_EMAILS} lower-priority items not shown."
        emails = emails[:MAX_EMAILS]
        logger.info(f"Pre-processing: capped emails to {MAX_EMAILS}, {overflow_note}")

    return emails, overflow_note


def _priority_match(emails: list[FlaggedEmail], priorities: str) -> list[FlaggedEmail]:
    """Tag emails that match keywords from the priorities list."""
    if not priorities:
        return emails

    # Extract meaningful keywords/phrases from priorities (lines with content)
    priority_lines = [
        line.strip() for line in priorities.splitlines()
        if line.strip() and len(line.strip()) > 3
    ]

    # Build keyword list: use each priority line as a potential match
    # Also extract individual significant words (4+ chars)
    keywords = []
    for line in priority_lines:
        keywords.append(line.lower())
        for word in re.findall(r'\b[a-zA-Z]{4,}\b', line):
            # Skip very common words
            if word.lower() not in {
                "with", "from", "that", "this", "have", "will", "been",
                "more", "about", "would", "their", "there", "which", "could",
                "should", "other", "than", "into", "some", "what", "your",
                "when", "them", "then", "each", "make", "like", "just",
                "over", "also", "back", "after", "work", "only", "most",
                "very", "here", "need", "want", "does", "done",
            }:
                keywords.append(word.lower())

    # Deduplicate while preserving order (longer phrases first)
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)

    matched_count = 0
    for email in emails:
        search_text = f"{email.subject} {email.sender_name} {email.sender}".lower()
        matches = []
        for kw in unique_keywords:
            if kw in search_text:
                matches.append(kw)
        if matches:
            # Use the longest match as the best priority reference
            best_match = max(matches, key=len)
            email.snippet = f"[MATCHES PRIORITY: '{best_match}'] {email.snippet}"
            matched_count += 1

    if matched_count:
        logger.info(f"Pre-processing: matched {matched_count} email(s) to priorities")
    return emails


def _group_emails_by_priority(emails: list[FlaggedEmail]) -> str:
    """Pre-group emails into high/medium/low sections as formatted text."""
    groups = {"high": [], "medium": [], "low": []}
    for e in emails:
        groups.get(e.urgency, groups["low"]).append(e)

    lines = []
    for level, label in [("high", "HIGH PRIORITY"), ("medium", "MEDIUM PRIORITY"), ("low", "LOW PRIORITY")]:
        group = groups[level]
        if not group:
            continue
        lines.append(f"--- {label} ({len(group)} items) ---")
        for e in group:
            lines.append(
                f"- [thread:{e.thread_id}] Subject: {e.subject}, From: {e.sender_name} <{e.sender}>, "
                f"Age: {e.age_days} days, Reason: {e.reason}, Urgency: {e.urgency}, "
                f"Snippet: {e.snippet}"
            )
        lines.append("")

    return "\n".join(lines) if lines else "None — inbox looks clean."


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

        # Compute day label
        if meeting_date == today:
            day_label = "TODAY"
        elif meeting_date == tomorrow:
            day_label = "TOMORROW"
        else:
            day_label = m.start.strftime("%A").upper()  # e.g., "WEDNESDAY"

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
    """Format preferences as text for XML tag."""
    rules = prefs.get("rules", [])
    if not rules:
        return ""
    return "Learned rules:\n" + "\n".join(f"- {r}" for r in rules)


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

    Returns dict with keys: emails_xml, calendar_xml, priorities_xml,
    preferences_xml, memories_xml, dismissed_xml, overflow_note
    """
    logger.info(f"Pre-processing: starting with {len(flagged_emails)} emails, {len(meetings)} meetings")

    # (a) Hard filter dismissed thread IDs
    emails = _hard_filter_dismissed(flagged_emails)

    # (b) Fuzzy dismiss tagging — tag emails from recently dismissed senders
    emails = _fuzzy_dismiss_tag(emails, prefs)

    # (c) Priority matching — tag emails that match priority keywords
    emails = _priority_match(emails, priorities)

    # (d) Cap email count
    emails, overflow_note = _cap_emails(emails)

    # (e) Pre-group by priority into formatted text
    emails_xml = _group_emails_by_priority(emails)

    # (f) Calendar pre-formatting with day labels
    calendar_xml = _format_calendar(meetings, local_now)

    # Format other sections
    priorities_xml = priorities if priorities else ""
    preferences_xml = _format_preferences(prefs)
    memories_xml = memories_context if memories_context else ""
    dismissed_xml = dismissed_context if dismissed_context else ""

    logger.info(
        f"Pre-processing complete: {len(emails)} emails (grouped), "
        f"{len(meetings)} meetings formatted"
    )

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

        # 1. Scan emails
        logger.info("Scanning inbox...")
        flagged_emails = scan_inbox()
        logger.info(f"Found {len(flagged_emails)} flagged emails")

        # 2. Load preferences, priorities, and memories
        prefs = load_preferences()
        logger.info("Fetching priorities...")
        priorities = fetch_priorities()
        memories_context = get_memories_for_prompt()
        dismissed_context = get_dismissed_context()

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
