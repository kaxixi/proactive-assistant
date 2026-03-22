"""Claude API — generates natural language digests and analyzes importance."""

import logging
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, ENABLE_EMAIL

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

## Open loop rules
- Each open loop represents a topic needing attention. A loop may contain multiple email threads from different senders about the same topic.
- Suggest a specific action for every loop you mention (reply, follow up, archive, ignore).
- Weekday digests: 5-10 open loops max, grouped by priority.
- Weekend digests: 3-5 loops max, framed as "if you have time."
- Week-ahead digests: focus on loops with upcoming deadlines or that have been sitting too long.
- Reference loops by their title, not individual thread IDs.
- Each loop has a number (#1, #2, etc.). ALWAYS include the number when mentioning a loop so Erez can reply with "1 handled" or "tell me more about 3". Format: "#1 Title — action suggestion"

## Accuracy rules — CRITICAL
- Your summaries MUST accurately reflect the email content. Do NOT guess or infer context beyond what the loop summary and snippets say.
- Pay attention to WHO sent the last message. If Erez sent the last message and the loop says "waiting for response", do NOT tell Erez to reply — say the other person hasn't responded yet.
- If a meeting or deadline date mentioned in a loop is in the PAST, note that it has passed. Do not suggest actions on past events.
- When context is unclear, be honest: "I see a thread about [topic] from [sender] — is this still active?" This is BETTER than guessing wrong.
- When matching loops to priorities, match on the specific person or project, not just first names. "Jeff" in the priorities list is not necessarily the same as any Jeffrey in the inbox.

## Priority cross-referencing
- If Erez's priorities list is available, cross-reference loops and meetings against it.
- Call out anything that connects to a current priority explicitly (e.g., "this connects to your Templeton OFI priority").
- Only match when confident. If a name or topic is ambiguous (e.g., common first name matching multiple people), ask rather than assume.

## Memory usage
- Use recent context to avoid re-flagging resolved items and to reference ongoing situations naturally.
- FOLLOW-UP items in memory are things Erez explicitly asked to be reminded about. Always surface these prominently — they persist until he says they're done.
- Use historical context (weekly/monthly summaries) to understand longer-term patterns.

## Follow-up and person-level tracking
- Emails annotated with `[FOLLOW-UP REMINDER: ...]` have an active follow-up in memory — surface these prominently with the reminder context.
- A "FOLLOW-UP REMINDERS (no email thread)" section may appear after loops — these are commitments made outside email (e.g., WhatsApp). Include them in the digest naturally.

## Learned patterns
- Your <preferences> section contains patterns learned from past interactions.
- When you apply a learned pattern to deprioritize or highlight a loop, briefly note it
  the first time: "Deprioritizing this recruiting loop (you usually skip these) — correct
  me if I'm wrong."
- This lets Erez confirm or correct your judgment in real time.

## Dismissed loop handling
Re-flagging handled items may confuse Erez. He has a busy schedule and may not remember he already dealt with something — he might accidentally re-send an email or re-do work. When in doubt, skip it.
- Dismissed loops have already been filtered out in pre-processing.
- If the <dismissed_threads> section mentions recently dismissed topics, and you see a loop that looks related, do NOT flag it as an action item. Instead, mention it briefly: "I also see a loop about [topic] — looks related to what you already handled. Want me to dismiss it?"

## Example weekday digest

Here is an example of the desired tone, structure, and format:

---
📅 Today (Tuesday):
• 10:00 AM — Lab meeting (recurring)
• 2:00 PM — ⚡ ONE-TIME: Grant review with Sarah Chen — needs prep (5 attendees). Want me to pull up the proposal draft?

📅 Tomorrow:
• 9:00 AM — Faculty seminar (recurring)
• 3:30 PM — ⚡ ONE-TIME: Coffee with visiting speaker Dr. Liu

📬 Open loops needing attention:

🔴 High priority:
• #1 Arjun's HCRP application (3 threads, 9 days) — connects to your "Arjun LOR" priority. Includes emails from Arjun and CommunityForce. Suggest: reply to Arjun this morning, the rest will resolve.
• #2 Department chair re: curriculum committee (1 thread, 3 days) — Suggest: quick reply confirming attendance.

🟡 Medium:
• #3 SPSP conference logistics (2 threads, 6 days) — submission confirmation + hotel block. Suggest: archive the confirmation, book hotel if needed.

Reply with numbers to triage: "1 handled", "3 snooze", "tell me more about 2"

All other loops look handled. Have a good Tuesday! ☕
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
    sections.append(f"<open_loops>\n{emails_xml}\n</open_loops>")

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

    # Task-specific instructions — vary by digest type and whether email is enabled
    if ENABLE_EMAIL:
        task_instructions = {
            "weekday": """<task>
Generate a concise, friendly weekday morning digest.
- List ALL calendar events for today and tomorrow — every single one.
- ONE-TIME meetings should be highlighted with extra emphasis.
- Keep it focused — aim for 5-10 open loops max, grouped by priority.
- If there's nothing urgent, say so briefly and positively.
</task>""",
            "weekend": """<task>
Generate a relaxed weekend morning digest.
- Show what's on the calendar for the weekend.
- For open loops, focus on items that didn't get done during the week — frame them as "if you have time this weekend" rather than urgent.
- Keep the tone relaxed — it's the weekend.
- Don't push too many action items — pick the top 3-5 that would make Monday easier if handled.
</task>""",
            "week_ahead": """<task>
Generate a week-ahead planning digest for the coming week (Monday through Friday).
- For calendar: highlight ONE-TIME (non-recurring) meetings specifically — say things like "In addition to your usual recurring meetings, you have a one-time meeting with X on Tuesday, Y on Thursday."
- For recurring meetings, you can summarize them briefly ("your usual Monday/Wednesday meetings") rather than listing each one.
- For open loops: focus on items with upcoming deadlines this week, and anything that's been sitting too long.
- Cross-reference priorities: if a priority item has a deadline this week, call it out explicitly.
- Set the tone for the week — help Erez feel organized and on top of things.
- Keep it focused — this is a planning overview, not a detailed daily breakdown.
</task>""",
        }
        self_check = """<self_check>
Before finalizing, verify:
1. Every calendar event from the <calendar> section is included in your response.
2. No loops match dismissed topics from <dismissed_threads> (skip those).
3. Every loop you mention has a specific action suggestion.
4. Response is under 400 words.
</self_check>"""
    else:
        task_instructions = {
            "weekday": """<task>
Generate a concise, friendly weekday morning calendar digest.
- List ALL calendar events for today and tomorrow — every single one.
- ONE-TIME meetings should be highlighted with extra emphasis.
- For meetings that need prep, offer to help.
- If there's nothing unusual, say so briefly and positively.
</task>""",
            "weekend": """<task>
Generate a relaxed weekend morning calendar digest.
- Show what's on the calendar for the weekend.
- Keep the tone relaxed — it's the weekend.
</task>""",
            "week_ahead": """<task>
Generate a week-ahead calendar planning digest for the coming week (Monday through Friday).
- Highlight ONE-TIME (non-recurring) meetings specifically — say things like "In addition to your usual recurring meetings, you have a one-time meeting with X on Tuesday, Y on Thursday."
- For recurring meetings, you can summarize them briefly ("your usual Monday/Wednesday meetings") rather than listing each one.
- Cross-reference priorities if available.
- Set the tone for the week — help Erez feel organized and on top of things.
- Keep it focused — this is a planning overview, not a detailed daily breakdown.
</task>""",
        }
        self_check = """<self_check>
Before finalizing, verify:
1. Every calendar event from the <calendar> section is included in your response.
2. Response is under 300 words.
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
