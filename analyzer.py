"""Claude API — generates natural language digests and analyzes importance."""

import logging
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from email_monitor import FlaggedEmail
from calendar_digest import Meeting

logger = logging.getLogger(__name__)


def _get_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def generate_daily_digest(
    flagged_emails: list[FlaggedEmail],
    meetings: list[Meeting],
    preferences: dict = None,
    priorities: str = "",
    memories_context: str = "",
    dismissed_context: str = "",
) -> str:
    """Generate a natural language daily digest using Claude."""

    # Build context
    email_summary = []
    for e in flagged_emails:
        email_summary.append(
            f"- [thread:{e.thread_id}] Subject: {e.subject}, From: {e.sender_name} <{e.sender}>, "
            f"Age: {e.age_days} days, Reason: {e.reason}, Urgency: {e.urgency}, "
            f"Snippet: {e.snippet}"
        )

    meeting_summary = []
    for m in meetings:
        day = "Tomorrow" if m.is_tomorrow else "Today"
        recurrence = "ONE-TIME" if not m.is_recurring else "recurring"
        meeting_summary.append(
            f"- {day} {m.start.strftime('%I:%M %p')}: {m.summary}, "
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

    prompt = f"""You are Claudette, a proactive personal assistant for Erez, a behavioral science researcher.
Generate a concise, friendly morning digest based on this data.

EMAILS NEEDING ATTENTION:
{chr(10).join(email_summary) if email_summary else "None — inbox looks clean."}

CALENDAR (list ALL meetings — do not skip any):
{chr(10).join(meeting_summary) if meeting_summary else "No meetings today or tomorrow."}
{priorities_context}{pref_context}
{f"RECENT CONTEXT (from past interactions and digests — use this to avoid redundancy):{chr(10)}{memories_context}{chr(10)}" if memories_context else ""}
{f"PREVIOUSLY DISMISSED EMAILS (Erez already handled these — only re-flag if a genuinely NEW issue appeared in the same thread or from the same sender):{chr(10)}{dismissed_context}{chr(10)}" if dismissed_context else ""}
Guidelines:
- List ALL calendar events — every single one. Non-recurring (ONE-TIME) meetings should be highlighted with extra emphasis since Erez is more likely to miss them
- Lead with calendar, then most urgent email items
- For emails, suggest specific actions (reply, follow up, archive)
- For meetings that need prep, offer to help (e.g., "Want me to look anything up?")
- Keep it focused — aim for 5-10 actionable email items max, grouped by priority
- Be warm but concise — this is a Telegram message, not an essay
- Use emoji sparingly for visual structure
- If there's nothing urgent, say so briefly and positively
- Erez can reply to this message with feedback or questions
- If Erez's priorities list is available, cross-reference emails and meetings against it — highlight anything that connects to a current priority
- Use RECENT CONTEXT to avoid re-flagging resolved items and to reference ongoing situations naturally
- Check PREVIOUSLY DISMISSED EMAILS before flagging anything. If an email has the same thread ID as a dismissed thread, skip it — it's the exact same conversation. If the thread ID differs but the sender/topic matches a dismissal, use your judgment: only include it if there is clearly a NEW issue (e.g., a new charge, a new question). If it looks like the same issue Erez already handled, skip it.
- Use historical context (from weekly/monthly summaries) to understand longer-term patterns and avoid repeating issues that were resolved weeks ago.
"""

    response = _get_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text
