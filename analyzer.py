"""Claude API — generates natural language digests and analyzes importance."""

import logging
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from email_monitor import FlaggedEmail
from calendar_digest import Meeting

logger = logging.getLogger(__name__)


def _get_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _build_shared_context(
    flagged_emails: list[FlaggedEmail],
    meetings: list[Meeting],
    preferences: dict = None,
    priorities: str = "",
    memories_context: str = "",
    dismissed_context: str = "",
) -> dict:
    """Build the shared data sections used by all digest types."""
    email_summary = []
    for e in flagged_emails:
        email_summary.append(
            f"- [thread:{e.thread_id}] Subject: {e.subject}, From: {e.sender_name} <{e.sender}>, "
            f"Age: {e.age_days} days, Reason: {e.reason}, Urgency: {e.urgency}, "
            f"Snippet: {e.snippet}"
        )

    meeting_lines = []
    for m in meetings:
        day_name = m.start.strftime("%A")  # Monday, Tuesday, etc.
        day_label = "Today" if not m.is_tomorrow and m.start.date() == meetings[0].start.date() else day_name
        recurrence = "ONE-TIME" if not m.is_recurring else "recurring"
        meeting_lines.append(
            f"- {day_label} {m.start.strftime('%I:%M %p')}: {m.summary}, "
            f"Attendees: {len(m.attendees)}, Needs prep: {m.needs_prep}, "
            f"Type: {recurrence}"
        )

    pref_context = ""
    if preferences and preferences.get("rules"):
        pref_context = (
            "\nUser preferences (learned from past feedback):\n"
            + "\n".join(f"- {r}" for r in preferences["rules"])
        )

    priorities_context = ""
    if priorities:
        priorities_context = f"\nEREZ'S CURRENT PRIORITIES:\n{priorities}\n"

    return {
        "emails": chr(10).join(email_summary) if email_summary else "None — inbox looks clean.",
        "meetings": chr(10).join(meeting_lines) if meeting_lines else "No meetings.",
        "priorities": priorities_context,
        "prefs": pref_context,
        "memories": f"RECENT CONTEXT (from past interactions and digests — use this to avoid redundancy):\n{memories_context}\n" if memories_context else "",
        "dismissed": f"PREVIOUSLY DISMISSED EMAILS (Erez already handled these — only re-flag if a genuinely NEW issue appeared):\n{dismissed_context}\n" if dismissed_context else "",
    }


SHARED_GUIDELINES = """- Lead with calendar, then most urgent email items
- For emails, suggest specific actions (reply, follow up, archive)
- For meetings that need prep, offer to help (e.g., "Want me to look anything up?")
- Be warm but concise — this is a Telegram message, not an essay
- Use emoji sparingly for visual structure
- Erez can reply to this message with feedback or questions
- If Erez's priorities list is available, cross-reference emails and meetings against it — highlight anything that connects to a current priority
- Use RECENT CONTEXT to avoid re-flagging resolved items and to reference ongoing situations naturally
- Check PREVIOUSLY DISMISSED EMAILS before flagging anything. If an email has the same thread ID as a dismissed thread, skip it — it's the exact same conversation. If the thread ID differs but the sender/topic matches a dismissal, use your judgment: only include it if there is clearly a NEW issue. If it looks like the same issue Erez already handled, skip it.
- Use historical context (from weekly/monthly summaries) to understand longer-term patterns and avoid repeating issues that were resolved weeks ago."""


WEEKDAY_GUIDELINES = """- List ALL calendar events for today and tomorrow — every single one. Non-recurring (ONE-TIME) meetings should be highlighted with extra emphasis since Erez is more likely to miss them
- Keep it focused — aim for 5-10 actionable email items max, grouped by priority
- If there's nothing urgent, say so briefly and positively"""


WEEKEND_GUIDELINES = """- Show what's on the calendar for the weekend
- For emails, focus on items that didn't get done during the week — frame them as "if you have time this weekend" rather than urgent
- Keep the tone relaxed — it's the weekend
- Don't push too many action items — pick the top 3-5 that would make Monday easier if handled"""


WEEK_AHEAD_GUIDELINES = """- Give a preview of the FULL coming week (Monday through Friday)
- For calendar: highlight ONE-TIME (non-recurring) meetings specifically — say things like "In addition to your usual recurring meetings, you have a one-time meeting with X on Tuesday, Y on Thursday"
- For recurring meetings, you can summarize them briefly ("your usual Monday/Wednesday meetings") rather than listing each one
- For emails: focus on items with upcoming deadlines this week, and anything that's been sitting too long
- Cross-reference priorities: if a priority item has a deadline this week, call it out explicitly (e.g., "the Templeton OFI which you indicated you'd be applying for is due Thursday")
- Set the tone for the week — help Erez feel organized and on top of things
- Keep it focused — this is a planning overview, not a detailed daily breakdown"""


def generate_daily_digest(
    flagged_emails: list[FlaggedEmail],
    meetings: list[Meeting],
    preferences: dict = None,
    priorities: str = "",
    memories_context: str = "",
    dismissed_context: str = "",
    digest_type: str = "weekday",
) -> str:
    """Generate a natural language digest using Claude.

    digest_type: "weekday" (Mon-Fri), "weekend" (Saturday), "week_ahead" (Sunday)
    """
    ctx = _build_shared_context(
        flagged_emails, meetings, preferences, priorities,
        memories_context, dismissed_context,
    )

    type_labels = {
        "weekday": "Generate a concise, friendly weekday morning digest.",
        "weekend": "Generate a relaxed weekend morning digest. Focus on the weekend schedule and any items from the week that Erez might want to tackle if he has time.",
        "week_ahead": "Generate a week-ahead planning digest for the coming week (Monday through Friday). Help Erez start the week feeling organized.",
    }

    type_guidelines = {
        "weekday": WEEKDAY_GUIDELINES,
        "weekend": WEEKEND_GUIDELINES,
        "week_ahead": WEEK_AHEAD_GUIDELINES,
    }

    prompt = f"""You are Claudette, a proactive personal assistant for Erez, a behavioral science researcher.
{type_labels.get(digest_type, type_labels["weekday"])}

EMAILS NEEDING ATTENTION:
{ctx["emails"]}

CALENDAR:
{ctx["meetings"]}
{ctx["priorities"]}{ctx["prefs"]}
{ctx["memories"]}
{ctx["dismissed"]}
Guidelines:
{SHARED_GUIDELINES}
{type_guidelines.get(digest_type, WEEKDAY_GUIDELINES)}
"""

    response = _get_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text
