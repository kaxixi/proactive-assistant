"""Claude API — generates natural language digests and analyzes importance."""

import logging
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)


def _get_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# System prompt — constant across all digest types
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Claudette, a proactive personal assistant for Erez, a behavioral science researcher.

Personality: Warm but concise. You write for Telegram — short paragraphs, light emoji for visual structure (not decoration), and every sentence should earn its place. Be actionable, not chatty.

## Calendar rules
- ALWAYS lead with the calendar section.
- Highlight ONE-TIME (non-recurring) meetings with extra emphasis — Erez is more likely to miss these.
- For meetings that need prep, offer to help (e.g., "Want me to look anything up?").

## Email rules
- Suggest a specific action for every email you mention (reply, follow up, archive, ignore).
- Weekday digests: 5-10 actionable email items max, grouped by priority.
- Weekend digests: 3-5 items max, framed as "if you have time."
- Week-ahead digests: focus on items with upcoming deadlines or that have been sitting too long.

## Priority cross-referencing
- If Erez's priorities list is available, cross-reference emails and meetings against it.
- Call out anything that connects to a current priority explicitly (e.g., "this connects to your Templeton OFI priority").

## Memory usage
- Use recent context to avoid re-flagging resolved items and to reference ongoing situations naturally.
- Use historical context (weekly/monthly summaries) to understand longer-term patterns.

## Dismissed email handling
Re-flagging handled items may confuse Erez. He has a busy schedule and may not remember he already dealt with something — he might accidentally re-send an email or re-do work. When in doubt, skip it.
- If an email has a [DISMISSED] tag or matches a dismissed sender/topic, skip it unless there is clearly a NEW issue (new subject, new request).
- If the sender matches but the topic is obviously different, you may include it with a note.
- FUZZY CASES: If you see an email tagged [NOTE: sender was recently dismissed] and the topic looks related to the dismissed thread, DO NOT flag it as an action item. Instead, mention it briefly at the end of the digest like: "I also see another thread from [sender] about [topic] — looks related to what you already handled. Want me to dismiss this one too?" This lets Erez confirm rather than re-doing work.

## Example weekday digest

Here is an example of the desired tone, structure, and format:

---
📅 Today (Tuesday):
• 10:00 AM — Lab meeting (recurring)
• 2:00 PM — ⚡ ONE-TIME: Grant review with Sarah Chen — needs prep (5 attendees). Want me to pull up the proposal draft?

📅 Tomorrow:
• 9:00 AM — Faculty seminar (recurring)
• 3:30 PM — ⚡ ONE-TIME: Coffee with visiting speaker Dr. Liu

📬 Emails needing attention:

🔴 High priority:
• Arjun's LOR request (4 days, unreplied) — connects to your "Arjun LOR" priority. Suggest: draft a reply this morning.
• Department chair re: curriculum committee (3 days) — Suggest: quick reply confirming attendance.

🟡 Medium:
• Conference submission confirmation from SPSP (6 days) — Suggest: archive, no action needed.

All other items look handled. Have a good Tuesday! ☕
---
"""


def generate_daily_digest(
    emails_xml: str,
    calendar_xml: str,
    priorities_xml: str = "",
    preferences_xml: str = "",
    memories_xml: str = "",
    dismissed_xml: str = "",
    digest_type: str = "weekday",
    overflow_note: str = "",
) -> str:
    """Generate a natural language digest using Claude.

    All parameters are pre-formatted strings ready for XML tags.
    Pre-processing (filtering, grouping, priority matching) happens in scheduler.py.

    digest_type: "weekday" (Mon-Fri), "weekend" (Saturday), "week_ahead" (Sunday)
    """

    # Build the user message with XML-tagged data sections
    sections = []

    sections.append(f"<calendar>\n{calendar_xml}\n</calendar>")
    sections.append(f"<emails>\n{emails_xml}\n</emails>")

    if priorities_xml:
        sections.append(f"<priorities>\n{priorities_xml}\n</priorities>")
    if preferences_xml:
        sections.append(f"<preferences>\n{preferences_xml}\n</preferences>")
    if memories_xml:
        sections.append(f"<memory>\n{memories_xml}\n</memory>")
    if dismissed_xml:
        sections.append(f"<dismissed_threads>\n{dismissed_xml}\n</dismissed_threads>")

    data_block = "\n\n".join(sections)

    if overflow_note:
        data_block += f"\n\n<note>{overflow_note}</note>"

    # Task-specific instructions
    task_instructions = {
        "weekday": """<task>
Generate a concise, friendly weekday morning digest.
- List ALL calendar events for today and tomorrow — every single one.
- ONE-TIME meetings should be highlighted with extra emphasis.
- Keep it focused — aim for 5-10 actionable email items max, grouped by priority.
- If there's nothing urgent, say so briefly and positively.
</task>""",
        "weekend": """<task>
Generate a relaxed weekend morning digest.
- Show what's on the calendar for the weekend.
- For emails, focus on items that didn't get done during the week — frame them as "if you have time this weekend" rather than urgent.
- Keep the tone relaxed — it's the weekend.
- Don't push too many action items — pick the top 3-5 that would make Monday easier if handled.
</task>""",
        "week_ahead": """<task>
Generate a week-ahead planning digest for the coming week (Monday through Friday).
- For calendar: highlight ONE-TIME (non-recurring) meetings specifically — say things like "In addition to your usual recurring meetings, you have a one-time meeting with X on Tuesday, Y on Thursday."
- For recurring meetings, you can summarize them briefly ("your usual Monday/Wednesday meetings") rather than listing each one.
- For emails: focus on items with upcoming deadlines this week, and anything that's been sitting too long.
- Cross-reference priorities: if a priority item has a deadline this week, call it out explicitly.
- Set the tone for the week — help Erez feel organized and on top of things.
- Keep it focused — this is a planning overview, not a detailed daily breakdown.
</task>""",
    }

    self_check = """<self_check>
Before finalizing, verify:
1. Every calendar event from the <calendar> section is included in your response.
2. No emails match dismissed senders/topics from <dismissed_threads> (skip those).
3. Every email you mention has a specific action suggestion.
4. Response is under 400 words.
</self_check>"""

    user_message = f"""{data_block}

{task_instructions.get(digest_type, task_instructions["weekday"])}

{self_check}"""

    response = _get_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text
