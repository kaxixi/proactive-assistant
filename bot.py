"""Telegram bot — sends digests and handles interactive replies."""

import logging

import anthropic
from telegram import Bot, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, CLAUDE_MODEL, ENABLE_EMAIL

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

import state as _state

# Multi-turn conversation state
_conversation_history: list[dict] = []
_MAX_HISTORY = 5
_last_interaction_time: float = 0
# Highest scheduler-message timestamp already injected into _conversation_history
# as an assistant turn. Keeps replies to digests/memory-reviews stitched into
# the conversation thread instead of only living in the system prompt.
_last_scheduler_inject_ts: float = 0

# Cache for fetched full threads (cleared on new digest)
_thread_cache: dict[str, str] = {}


def _save_digest_loops(loops_map: dict[int, str]):
    """Persist the digest's numbered-loop map for the bot to read back."""
    session = _state.get_section("session") or {}
    session["digest_loop_numbers"] = {str(k): v for k, v in loops_map.items()}
    _state.set_section("session", session)


def _load_digest_loops() -> dict[int, str]:
    session = _state.get_section("session") or {}
    numbers = session.get("digest_loop_numbers") or {}
    try:
        return {int(k): v for k, v in numbers.items()}
    except (ValueError, TypeError):
        return {}


def _save_scheduler_message(text: str, label: str = "digest"):
    """Persist a message sent by the scheduler so the bot can include it as context.

    Keeps the last 3 messages (newest first) to cover digest + review + any extras.
    """
    import time as _time
    session = _state.get_section("session") or {}
    messages = session.get("last_scheduler_messages") or []
    messages.insert(0, {"label": label, "text": text, "ts": _time.time()})
    session["last_scheduler_messages"] = messages[:3]
    _state.set_section("session", session)


def _load_scheduler_messages() -> list[dict]:
    """Load recent scheduler messages from the unified state."""
    session = _state.get_section("session") or {}
    return session.get("last_scheduler_messages") or []

# Tool definitions for Claude
_BASE_TOOLS = [
    {
        "name": "confirm_rule",
        "description": (
            "Confirm an unconfirmed structured rule (listed in <unconfirmed_rules>). "
            "Use when Erez approves a rule compiled from his recent feedback. "
            "Flips the rule to confirmed; future matches are silent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "The rule id, e.g. 'r_abc123'."},
            },
            "required": ["rule_id"],
        },
    },
    {
        "name": "delete_rule",
        "description": (
            "Delete a structured rule by id. Use when Erez rejects an "
            "unconfirmed rule or asks to remove an existing one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "The rule id to remove."},
            },
            "required": ["rule_id"],
        },
    },
    {
        "name": "forget_memory",
        "description": (
            "Delete stored memory entries whose content matches the query "
            "(case-insensitive substring). Use this when Erez asks you to "
            "forget, clear, remove, or clean up memories — especially when "
            "responding to a memory review where he confirms stale facts, "
            "duplicates, or outdated items can be dropped. Memories are "
            "DIFFERENT from open loops: loops are email topics (use "
            "dismiss_email), memories are stored facts/preferences/follow-ups "
            "(use this tool). Optionally restrict to specific memory types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring to match against memory content (e.g. 'Lachman Walmart', 'March meeting', 'Story Explorers')."},
                "types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["preference", "relationship", "follow_up", "fact", "resolved", "conversation_summary", "pending"]},
                    "description": "Optional: only forget memories of these types. Omit to consider all types.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_drive",
        "description": "Search Google Drive for files matching a query. Use when Erez asks to find a document, paper, or file in Drive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_dropbox",
        "description": "Search Dropbox for files matching a query. Use when Erez asks to find a file in Dropbox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    },
]

_EMAIL_TOOLS = [
    {
        "name": "search_gmail",
        "description": "Search Gmail for emails matching a query. Use when Erez asks to find, look up, or check on an email. Supports Gmail search syntax (e.g. 'from:someone subject:topic', 'is:unread', 'newer_than:7d').",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "dismiss_loops_by_number",
        "description": (
            "Dismiss one or more open loops by their digest number (as shown in "
            "<digest_loop_numbers>). ALWAYS use this tool — never dismiss_email — "
            "when Erez references loops by number ('1 handled', 'dismiss 3 and 5', "
            "'resolve 2, 4, 6'). Passes the numbers directly to the number→loop_id "
            "map in session state, which avoids the fuzzy title matching that "
            "dismiss_email does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The loop numbers to dismiss, e.g. [2, 3, 4, 6, 9, 10].",
                },
                "reason": {"type": "string", "description": "Why they're being dismissed (e.g. 'handled', 'not relevant', 'resolved')"},
            },
            "required": ["numbers", "reason"],
        },
    },
    {
        "name": "dismiss_email",
        "description": (
            "Dismiss an email topic (open loop) by a free-text query. Use ONLY "
            "when Erez refers to a topic by name rather than by number — e.g. "
            "'Cap One is handled' or 'dismiss the Walmart thing'. If Erez gives "
            "a NUMBER from the digest, use dismiss_loops_by_number instead. "
            "Falls back to Gmail search for items not in the current loop set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to find the thread to dismiss (sender name, subject keywords, etc.)"},
                "reason": {"type": "string", "description": "Why it's being dismissed (e.g. 'handled', 'not relevant', 'resolved')"},
            },
            "required": ["query", "reason"],
        },
    },
]

TOOLS = _BASE_TOOLS + (_EMAIL_TOOLS if ENABLE_EMAIL else [])


def _is_authorized(update: Update) -> bool:
    return update.effective_chat.id == TELEGRAM_CHAT_ID


def _get_claude():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _search_gmail(query: str, max_results: int = 5) -> str:
    """Search Gmail and return formatted results."""
    import email.utils
    from googleapiclient.discovery import build
    from google_auth import get_credentials

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = resp.get("messages", [])

    if not messages:
        return f"No emails found matching '{query}'."

    lines = []
    for msg_meta in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_meta["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        header_map = {h["name"].lower(): h["value"] for h in headers}
        subject = header_map.get("subject", "(no subject)")
        sender = header_map.get("from", "unknown")
        date = header_map.get("date", "")
        snippet = msg.get("snippet", "")
        lines.append(f"From: {sender}\nSubject: {subject}\nDate: {date}\n{snippet}\n")

    return "\n---\n".join(lines)


def _dismiss_loops_by_number(numbers: list[int], reason: str) -> str:
    """Dismiss loops referenced by their digest number.

    Reads the number→loop_id map from session state (written by the
    scheduler or by /loops), then dismisses each loop directly — no
    fuzzy matching, no chance of drift across similarly-titled loops.
    """
    from memory import add_memories
    from open_loops import dismiss_loop as dismiss_loop_fn, get_loop_by_id

    digest_loops = _load_digest_loops()
    if not digest_loops:
        return (
            "I don't have a current loop-number map. Run /loops first to "
            "refresh the numbering, then try again."
        )

    dismissed_titles: list[str] = []
    missing: list[int] = []
    already_closed: list[int] = []

    for num in numbers:
        loop_id = digest_loops.get(int(num))
        if not loop_id:
            missing.append(num)
            continue
        target = get_loop_by_id(loop_id)
        if not target or target.status == "dismissed":
            already_closed.append(num)
            continue
        dismissed = dismiss_loop_fn(loop_id, reason)
        if not dismissed:
            missing.append(num)
            continue
        dismissed_titles.append(f"#{num} \"{dismissed.title}\"")
        tags = list(dismissed.tags) if dismissed.tags else []
        add_memories([{
            "type": "resolved",
            "content": f"Dismissed loop \"{dismissed.title}\" ({len(dismissed.thread_ids)} threads) — reason: {reason}",
            "tags": tags,
            "source": "dismiss",
        }])

    parts = []
    if dismissed_titles:
        parts.append(
            f"Dismissed {len(dismissed_titles)} loop(s) (reason: {reason}):\n"
            + "\n".join(f"  • {t}" for t in dismissed_titles)
        )
    if already_closed:
        parts.append(f"Already closed: {', '.join(f'#{n}' for n in already_closed)}")
    if missing:
        parts.append(
            f"No loop matched these numbers: {', '.join(f'#{n}' for n in missing)}. "
            "Run /loops to refresh the numbering."
        )
    return "\n\n".join(parts) if parts else "No loops dismissed."


def _dismiss_email(query: str, reason: str) -> str:
    """Dismiss an open loop or Gmail threads matching the query.

    First searches open loops (topic-level). If a matching loop is found,
    dismisses the entire loop (all threads at once). Falls back to Gmail
    search for items not in the latest scan.
    """
    from memory import add_memories

    # Step 1: Try to find a matching open loop
    from open_loops import find_loop_by_query, dismiss_loop as dismiss_loop_fn

    matched_loop = find_loop_by_query(query)
    if matched_loop:
        dismissed = dismiss_loop_fn(matched_loop.loop_id, reason)
        if dismissed:
            # Create a resolved memory with the loop's tags
            tags = list(dismissed.tags) if dismissed.tags else []
            # Also tag from the search query
            for word in query.split():
                cleaned = word.strip(".:,;!?()[]\"'")
                if len(cleaned) >= 3:
                    if cleaned[0].isupper():
                        tags.append(f"person:{cleaned}")
                    else:
                        tags.append(f"topic:{cleaned.lower()}")
            tags = list(dict.fromkeys(tags))

            add_memories([{
                "type": "resolved",
                "content": f"Dismissed loop \"{dismissed.title}\" ({len(dismissed.thread_ids)} threads) — reason: {reason}",
                "tags": tags,
                "source": "dismiss",
            }])

            thread_count = len(dismissed.thread_ids)
            return (
                f"Dismissed loop: \"{dismissed.title}\" "
                f"({thread_count} thread{'s' if thread_count != 1 else ''}, "
                f"reason: {reason}). All related emails won't appear in future digests."
            )

    # Step 2: Fall back to Gmail search (for items not in latest scan)
    from googleapiclient.discovery import build
    from google_auth import get_credentials
    from open_loops import dismiss_thread_as_loop
    import email.utils

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    # Search inbox only, with subject scope to avoid broad matches
    gmail_query = f"in:inbox subject:({query})"
    resp = service.users().messages().list(
        userId="me", q=gmail_query, maxResults=10
    ).execute()
    messages = resp.get("messages", [])

    # If subject search fails, try broader search
    if not messages:
        resp = service.users().messages().list(
            userId="me", q=f"in:inbox {query}", maxResults=10
        ).execute()
        messages = resp.get("messages", [])

    if not messages:
        return f"No emails or loops found matching '{query}'. Could not dismiss."

    # Get sender from the first match
    first_msg = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="metadata",
        metadataHeaders=["Subject", "From"],
    ).execute()
    first_headers = first_msg.get("payload", {}).get("headers", [])
    first_from = next((h["value"] for h in first_headers if h["name"].lower() == "from"), "")
    sender_name, primary_sender = email.utils.parseaddr(first_from)
    primary_sender_lower = primary_sender.lower()

    # Dismiss all threads from the same sender
    dismissed_subjects = []
    seen_thread_ids = set()
    for msg_meta in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_meta["id"], format="metadata",
            metadataHeaders=["Subject", "From"],
        ).execute()
        thread_id = msg["threadId"]
        if thread_id in seen_thread_ids:
            continue

        headers = msg.get("payload", {}).get("headers", [])
        from_header = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
        _, sender_email_addr = email.utils.parseaddr(from_header)

        if sender_email_addr.lower() == primary_sender_lower:
            subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "")
            dismiss_thread_as_loop(
                thread_id=thread_id,
                subject=subject,
                sender=primary_sender_lower,
                reason=reason,
            )
            dismissed_subjects.append(subject)
            seen_thread_ids.add(thread_id)

    if not dismissed_subjects:
        return f"No emails found matching '{query}'. Could not dismiss."

    # Create a resolved memory
    display_name = sender_name.strip() if sender_name.strip() else primary_sender_lower
    tags = [f"person:{display_name}"]
    for word in query.split():
        cleaned = word.strip(".:,;!?()[]\"'")
        if len(cleaned) >= 3:
            if cleaned[0].isupper():
                tags.append(f"person:{cleaned}")
            else:
                tags.append(f"topic:{cleaned.lower()}")
    for subject in dismissed_subjects:
        for word in subject.split():
            cleaned = word.strip(".:,;!?()[]\"'").lower()
            if len(cleaned) >= 4 and cleaned.isalpha():
                tags.append(f"topic:{cleaned}")
    tags = list(dict.fromkeys(tags))

    summary = "; ".join(f'"{s}"' for s in dismissed_subjects[:3])
    add_memories([{
        "type": "resolved",
        "content": f"Dismissed emails from {display_name}: {summary} — reason: {reason}",
        "tags": tags,
        "source": "dismiss",
    }])

    count = len(dismissed_subjects)
    sender_display = display_name or "unknown sender"
    if count == 1:
        return f"Dismissed thread: \"{dismissed_subjects[0]}\" (reason: {reason}). It won't appear in future digests."
    return f"Dismissed {count} threads from {sender_display} (reason: {reason}): " + "; ".join(f"\"{s}\"" for s in dismissed_subjects)


def _execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if tool_name == "search_drive":
            from drive_search import search_files, format_drive_results
            files = search_files(tool_input["query"])
            return format_drive_results(files)
        elif tool_name == "search_dropbox":
            from dropbox_search import search_files, format_dropbox_results
            files = search_files(tool_input["query"])
            return format_dropbox_results(files)
        elif tool_name == "search_gmail":
            return _search_gmail(tool_input["query"])
        elif tool_name == "dismiss_loops_by_number":
            return _dismiss_loops_by_number(
                tool_input["numbers"], tool_input["reason"],
            )
        elif tool_name == "dismiss_email":
            return _dismiss_email(tool_input["query"], tool_input["reason"])
        elif tool_name == "confirm_rule":
            from rules import confirm_rule as _confirm_rule, describe_rule
            rule = _confirm_rule(tool_input["rule_id"])
            return (
                f"Confirmed: {describe_rule(rule)}"
                if rule
                else f"No rule found with id {tool_input['rule_id']}."
            )
        elif tool_name == "delete_rule":
            from rules import remove_rule
            ok = remove_rule(tool_input["rule_id"])
            return (
                f"Deleted rule {tool_input['rule_id']}."
                if ok
                else f"No rule found with id {tool_input['rule_id']}."
            )
        elif tool_name == "forget_memory":
            from memory import forget_memories
            count, sample = forget_memories(
                tool_input["query"], tool_input.get("types"),
            )
            if count == 0:
                return f"No memories matched '{tool_input['query']}'. Nothing forgotten."
            preview = "; ".join(f'"{s[:80]}"' for s in sample)
            extra = f" (showing first {len(sample)} of {count})" if count > len(sample) else ""
            return f"Forgot {count} memor{'y' if count == 1 else 'ies'}: {preview}{extra}"
        else:
            return f"Unknown tool: {tool_name}"
    except Exception as e:
        logger.error(f"Tool {tool_name} failed: {e}")
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Shared system prompt builder
# ---------------------------------------------------------------------------

def _build_system_prompt(extra_instructions: str = "") -> str:
    """Build the system prompt shared by handle_message and handle_document.

    Args:
        extra_instructions: additional instructions appended for specific contexts
            (e.g., document handling).
    """
    from memory import get_preference_memories
    pref_memories = get_preference_memories()
    rules_text = "\n".join(f"- {m['content']}" for m in pref_memories) or "None yet"

    # Inject recent scheduler messages (digest, memory review, etc.) so the bot
    # has context when the user replies to them.
    digest_section = ""
    scheduler_msgs = _load_scheduler_messages()
    if scheduler_msgs:
        parts = []
        for msg in scheduler_msgs:
            parts.append(f"<scheduler_message label=\"{msg['label']}\">\n{msg['text']}\n</scheduler_message>")
        digest_section = "\n" + "\n".join(parts) + "\n"

    from memory import get_memories_for_prompt
    memory_context = get_memories_for_prompt()
    memory_section = ""
    if memory_context:
        memory_section = f"\n<memory>\n{memory_context}\n</memory>\n"

    # Build numbered loop reference for the system prompt (read from disk —
    # the mapping is written by the scheduler process, read by the bot process)
    loop_ref = ""
    digest_loops = _load_digest_loops()
    if digest_loops:
        from open_loops import get_loop_by_id
        ref_lines = []
        for num, lid in sorted(digest_loops.items()):
            loop = get_loop_by_id(lid)
            if loop:
                ref_lines.append(f"  #{num} = \"{loop.title}\" (loop_id: {lid})")
        if ref_lines:
            loop_ref = "\n<digest_loop_numbers>\n" + "\n".join(ref_lines) + "\n</digest_loop_numbers>\n"

    # Surface unconfirmed rules so Claude can ask Erez to confirm them or
    # act on a confirmation/revert reply in the natural course of conversation.
    rules_ref = ""
    try:
        from rules import get_unconfirmed_rules, describe_rule
        unconfirmed = get_unconfirmed_rules()
        if unconfirmed:
            lines = [f"  {r['id']}: {describe_rule(r)}" for r in unconfirmed]
            rules_ref = "\n<unconfirmed_rules>\n" + "\n".join(lines) + "\n</unconfirmed_rules>\n"
    except Exception:
        pass

    if ENABLE_EMAIL:
        dismiss_instructions = """- When Erez says something is handled, resolved, done, taken care of, or not relevant — IMMEDIATELY dismiss the matching loop(s). Without dismissing, the item will reappear in future digests; Erez may not remember he already dealt with it and might accidentally re-send an email or re-do work he's already completed.
  - If Erez references loops by NUMBER (e.g., "1 handled", "dismiss 3 and 5", "resolve 2, 4, 6, 9, 10"): use the dismiss_loops_by_number tool with the full list of numbers in a SINGLE call. Do NOT map numbers to titles and call dismiss_email — that path does fuzzy title matching and can drift onto the wrong loop.
  - If Erez references a loop by free text ("Cap One is done", "dismiss the Walmart thing"): use dismiss_email with a descriptive query.
  Examples:
  - "1 handled" → dismiss_loops_by_number(numbers=[1], reason="handled")
  - "1 and 3 handled" → dismiss_loops_by_number(numbers=[1, 3], reason="handled")
  - "Resolve 2, 4, 6, 9" → dismiss_loops_by_number(numbers=[2, 4, 6, 9], reason="resolved")
  - "Cap One is handled" → dismiss_email(query="Cap One", reason="handled")
  - "Not relevant" / "Don't need that" → dismiss with reason "not relevant"
  When in doubt about whether Erez means to dismiss, dismiss it. It's better to dismiss and have him re-flag than to keep nagging about handled items."""
        tools_description = "You have tools to search Gmail, Google Drive, and Dropbox, to dismiss open loops (email topics) from future digests, and to forget stale stored memories."
    else:
        dismiss_instructions = ""
        tools_description = "You have tools to search Google Drive and Dropbox, and to forget stale stored memories."

    system_prompt = f"""You are Claudette, a proactive personal assistant for a behavioral science researcher named Erez.
You communicate via Telegram.

<preferences>
{rules_text}
</preferences>
{digest_section}{memory_section}{loop_ref}{rules_ref}
{tools_description}

Instructions:
- Respond warmly and concisely (this is Telegram, not email).
{dismiss_instructions}
- If he's asking a question, answer it directly.
- If he's asking you to find a file or look something up, use the search tools.
- Use your memory context to maintain continuity — reference past conversations naturally, avoid re-asking about things you already know.
- When Erez confirms cleanup of items raised in a memory review (stale facts, duplicates, contradictions, resolved follow-ups), use the forget_memory tool — NOT dismiss_email. Memory entries and email loops are different stores; dismiss_email only acts on loops.
- Extract LASTING preference rules from his feedback. Return them on lines starting with "RULE:" — these will be saved automatically.
  IMPORTANT: Only create rules for truly PERMANENT preferences (e.g., "Desiree's emails are always important", "Skip all Vercel notifications"). Do NOT create rules for temporary situations like "remind me to reply to Tim" — those are follow_up memories, not rules. The memory extraction system handles follow_ups automatically.
- If the prompt contains <unconfirmed_rules>, these are structured rules compiled from Erez's recent feedback that are operating in dry-run (the first few matches are logged). If Erez replies with confirmation ("yes", "keep", "good", "confirmed"), call confirm_rule with the rule id. If he rejects ("no", "revert", "delete", "undo", "that's wrong"), call delete_rule with the rule id. Mention the rule's match and action in your reply so he knows what he's confirming.
{extra_instructions}"""

    return system_prompt


_COMMANDS_TEXT = (
    "Commands:\n"
    "/digest — generate a digest right now\n"
    "/loops — list your open email loops (numbered)\n"
    "/loopcleanup — auto-close loops you've already engaged with in Gmail\n"
    "/memoryreview — review stored memories for stale or contradictory entries\n"
    "/rules — list structured rules (ingestion filters etc.)\n"
    "/availability [this/next week] — show free meeting slots\n"
    "/morningavailability [this/next week] — morning slots only\n"
    "/search <query> — search Drive and Dropbox\n"
    "/status — check which services are connected\n"
    "/commands — show this list\n"
    "/help — same as /commands\n\n"
    "You can also just reply to any message with feedback or questions."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    mode = "inbox and calendar" if ENABLE_EMAIL else "calendar"
    await update.message.reply_text(
        f"Hey! I'm Claudette, your proactive assistant. I'll send you daily "
        f"digests about your {mode}.\n\n{_COMMANDS_TEXT}"
    )


async def cmd_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(_COMMANDS_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    lines = ["Service status:"]
    lines.append("✓ Telegram — connected")
    lines.append("✓ Gmail — connected")
    lines.append("✓ Calendar — connected")
    lines.append("✓ Claude API — connected")
    lines.append("✓ Google Drive — connected")
    lines.append("✓ Dropbox — connected")
    from memory import get_preference_memories
    lines.append(f"✓ Learned preferences: {len(get_preference_memories())}")
    await update.message.reply_text("\n".join(lines))


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text("Running digest now...")
    from scheduler import run_daily_digest
    await run_daily_digest()


async def cmd_loopcleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-check every open loop against Gmail and auto-close the ones Erez
    has already engaged with (participated + not awaiting his reply)."""
    if not _is_authorized(update):
        return
    await update.message.reply_text("🧹 Checking open loops against Gmail...")
    from scheduler import _auto_close_handled_loops, _format_auto_close_summary
    closed = _auto_close_handled_loops()
    if not closed:
        await update.message.reply_text(
            "Nothing to auto-close — every open loop still has something waiting on you."
        )
        return
    await update.message.reply_text(_format_auto_close_summary(closed))


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all structured rules currently in effect."""
    if not _is_authorized(update):
        return
    from rules import list_rules_text
    text = list_rules_text()
    preamble = (
        "📐 Structured rules (ingestion / closure / priority).\n"
        "Reply 'confirm <id>' to confirm, 'delete <id>' to remove — or "
        "just say 'yes' / 'no' if there's a single unconfirmed rule pending.\n\n"
    )
    await update.message.reply_text(preamble + text)


async def cmd_memoryreview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger a memory review on demand (normally runs Sundays)."""
    if not _is_authorized(update):
        return
    await update.message.reply_text("🧠 Reviewing memories...")
    from memory import generate_memory_review, mark_review_done
    try:
        review = generate_memory_review()
    except Exception as e:
        await update.message.reply_text(f"Memory review failed: {type(e).__name__}: {e}")
        return
    if not review:
        await update.message.reply_text("Memory looks clean — nothing to flag.")
        return
    await update.message.reply_text(f"🧠 Memory check-in:\n\n{review}")
    mark_review_done()


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search both Drive and Dropbox."""
    if not _is_authorized(update):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /search <query>")
        return

    await update.message.reply_text(f"Searching for '{query}'...")

    from drive_search import search_files as drive_search, format_drive_results
    from dropbox_search import search_files as dbx_search, format_dropbox_results

    drive_files = drive_search(query)
    dbx_files = dbx_search(query)

    lines = []
    if drive_files:
        lines.append("📁 Google Drive:")
        lines.append(format_drive_results(drive_files))
    if dbx_files:
        lines.append("\n📦 Dropbox:")
        lines.append(format_dropbox_results(dbx_files))
    if not drive_files and not dbx_files:
        lines.append(f"No files found matching '{query}' in Drive or Dropbox.")

    await update.message.reply_text("\n".join(lines))


async def cmd_availability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available meeting slots for a week."""
    if not _is_authorized(update):
        return
    args = " ".join(context.args) if context.args else ""
    await update.message.reply_text("Checking calendar...")
    try:
        from availability import compute_availability
        result = compute_availability(args=args, morning_only=False)
        await update.message.reply_text(result, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Availability failed: {e}", exc_info=True)
        await update.message.reply_text(f"Error computing availability: {e}")


async def cmd_morningavailability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available morning slots for a week."""
    if not _is_authorized(update):
        return
    args = " ".join(context.args) if context.args else ""
    await update.message.reply_text("Checking calendar...")
    try:
        from availability import compute_availability
        result = compute_availability(args=args, morning_only=True)
        await update.message.reply_text(result, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Availability failed: {e}", exc_info=True)
        await update.message.reply_text(f"Error computing availability: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text replies — process via Claude with tool use."""
    if not _is_authorized(update):
        return

    global _conversation_history, _last_interaction_time, _last_scheduler_inject_ts

    user_text = update.message.text
    logger.info(f"Received message: {user_text[:100]}...")

    system_prompt = _build_system_prompt()

    try:
        import time
        # Check staleness — clear history if >30 minutes since last interaction
        if _last_interaction_time and (time.time() - _last_interaction_time > 1800):
            _conversation_history = []
        _last_interaction_time = time.time()

        # Inject any scheduler messages (digest, memory review, etc.) that
        # arrived since we last did so, as proper assistant turns. This makes
        # them feel like "what I just said" instead of background context, so
        # Erez's reply is correctly attributed.
        scheduler_msgs = _load_scheduler_messages()
        new_msgs = sorted(
            (m for m in scheduler_msgs if m.get("ts", 0) > _last_scheduler_inject_ts),
            key=lambda m: m.get("ts", 0),
        )
        for m in new_msgs:
            _conversation_history.append({"role": "assistant", "content": m["text"]})
            _last_scheduler_inject_ts = max(_last_scheduler_inject_ts, m.get("ts", 0))

        client = _get_claude()
        # Build messages with conversation history for multi-turn context
        messages = list(_conversation_history) + [{"role": "user", "content": user_text}]

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Handle tool use loop
        while response.stop_reason == "tool_use":
            tool_results = []
            assistant_content = response.content

            for block in assistant_content:
                if block.type == "tool_use":
                    logger.info(f"Tool call: {block.name}({block.input})")
                    result = _execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

        # Extract text response
        reply = ""
        for block in response.content:
            if hasattr(block, "text"):
                reply += block.text

        # Extract and save any preference rules as memories
        from memory import add_memories, extract_and_store
        lines = reply.split("\n")
        display_lines = []
        for line in lines:
            if line.strip().startswith("RULE:"):
                rule = line.strip().removeprefix("RULE:").strip()
                if rule:
                    add_memories([{"type": "preference", "content": rule, "tags": [], "source": "bot_rule"}])
                    logger.info(f"Learned new preference: {rule}")
            else:
                display_lines.append(line)

        display_text = "\n".join(display_lines).strip()
        if display_text:
            await update.message.reply_text(display_text)

        # Update conversation history for multi-turn
        _conversation_history.append({"role": "user", "content": user_text})
        _conversation_history.append({"role": "assistant", "content": reply})
        if len(_conversation_history) > _MAX_HISTORY * 2:
            _conversation_history = _conversation_history[-_MAX_HISTORY * 2:]

        # Extract memories from this conversation (non-blocking, non-fatal)
        try:
            conversation_log = f"User: {user_text}\nAssistant: {reply}"
            extract_and_store(conversation_log, source="bot")
        except Exception as mem_err:
            logger.warning(f"Memory extraction failed (non-fatal): {mem_err}")

        # Generate conversation summary if this was a substantive exchange
        # (tool use indicates multi-step interaction worth summarizing)
        if len(messages) > 1:  # had tool use rounds
            try:
                from memory import summarize_conversation
                # Build full conversation log including tool actions
                full_log_parts = [f"User: {user_text}"]
                for msg in messages[1:]:  # skip initial user message
                    if msg["role"] == "assistant":
                        for block in msg["content"]:
                            if hasattr(block, "text") and block.text:
                                full_log_parts.append(f"Assistant: {block.text}")
                            elif hasattr(block, "name"):
                                full_log_parts.append(f"Tool call: {block.name}({block.input})")
                full_log_parts.append(f"Assistant (final): {reply}")
                summarize_conversation("\n".join(full_log_parts))
            except Exception as sum_err:
                logger.warning(f"Conversation summary failed (non-fatal): {sum_err}")

    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        await update.message.reply_text(
            "Sorry, I hit an error processing that. I've logged your message "
            "and will factor it into future digests."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file attachments — download and read text-based files."""
    if not _is_authorized(update):
        return

    doc = update.message.document
    caption = update.message.caption or ""
    logger.info(f"Received document: {doc.file_name} ({doc.mime_type})")

    # Only handle text-based files
    text_types = {
        "text/plain", "text/markdown", "text/csv", "text/html",
        "application/json", "application/xml",
    }
    text_extensions = {".md", ".txt", ".csv", ".json", ".xml", ".py", ".js", ".html", ".yml", ".yaml"}

    file_ext = ""
    if doc.file_name:
        file_ext = "." + doc.file_name.rsplit(".", 1)[-1].lower() if "." in doc.file_name else ""

    if doc.mime_type not in text_types and file_ext not in text_extensions:
        await update.message.reply_text(
            f"I received {doc.file_name} but I can only read text-based files "
            f"(.md, .txt, .csv, .json, .py, etc.) for now."
        )
        return

    try:
        file = await doc.get_file()
        content_bytes = await file.download_as_bytearray()
        file_content = content_bytes.decode("utf-8")

        # Truncate very large files
        if len(file_content) > 10000:
            file_content = file_content[:10000] + "\n\n[...truncated...]"

        user_text = f"I'm sending you a file called '{doc.file_name}'."
        if caption:
            user_text += f" {caption}"
        user_text += f"\n\nFile contents:\n{file_content}"

        doc_extra = """
Additional instructions for document handling:
- If the file contains suggestions or preferences, acknowledge them and extract rules.
- Summarize the key points of the document concisely.
- If the document relates to an ongoing project or priority, connect it."""

        system_prompt = _build_system_prompt(extra_instructions=doc_extra)

        client = _get_claude()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text}],
        )

        reply = response.content[0].text

        from memory import add_memories, extract_and_store
        lines = reply.split("\n")
        display_lines = []
        for line in lines:
            if line.strip().startswith("RULE:"):
                rule = line.strip().removeprefix("RULE:").strip()
                if rule:
                    add_memories([{"type": "preference", "content": rule, "tags": [], "source": "bot_rule"}])
                    logger.info(f"Learned new preference: {rule}")
            else:
                display_lines.append(line)

        display_text = "\n".join(display_lines).strip()
        if display_text:
            await update.message.reply_text(display_text)

        # Extract memories from this conversation (non-fatal)
        try:
            extract_and_store(f"User sent file '{doc.file_name}': {caption}\nAssistant: {reply}", source="bot")
        except Exception as mem_err:
            logger.warning(f"Memory extraction failed (non-fatal): {mem_err}")

    except Exception as e:
        logger.error(f"Error reading document: {e}", exc_info=True)
        await update.message.reply_text(
            f"Sorry, I couldn't read {doc.file_name}: {e}"
        )


async def cmd_loops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current open loops dashboard."""
    if not _is_authorized(update):
        return
    from open_loops import get_open_loops, loop_age_days

    loops = get_open_loops()
    if not loops:
        await update.message.reply_text("No open loops — inbox looks clean!")
        return

    urgency_order = {"high": 0, "medium": 1, "low": 2}
    loops.sort(key=lambda l: (urgency_order.get(l.urgency, 2), -loop_age_days(l)))

    # Update the number→loop_id mapping so subsequent messages use these numbers
    loops_map = {}
    urgency_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = [f"📬 Open loops ({len(loops)}):\n"]
    for i, loop in enumerate(loops, 1):
        loops_map[i] = loop.loop_id
        emoji = urgency_emoji.get(loop.urgency, "⚪")
        senders = ", ".join(loop.senders[:2])
        snooze = " ⏰" if loop.snoozed_until else ""
        lines.append(
            f"#{i} {emoji} **{loop.title}**{snooze}\n"
            f"   opened {loop_age_days(loop)}d ago · {len(loop.thread_ids)} thread(s) · {senders}"
        )
    _save_digest_loops(loops_map)

    lines.append(f"\nReply to dismiss: \"1 handled\", \"3 snooze\", etc.")
    text = "\n".join(lines)
    for i in range(0, len(text), 4096):
        await update.message.reply_text(text[i:i + 4096])


async def send_message(text: str, include_buttons: bool = False, label: str = "digest"):
    """Send a message to the configured chat (used by scheduler)."""
    global _thread_cache
    _save_scheduler_message(text, label=label)
    _thread_cache = {}  # clear cache on new digest
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async with bot:
        if len(text) <= 4096:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        else:
            for i in range(0, len(text), 4096):
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=text[i:i + 4096]
                )

        # Store numbered loop references for conversational triage
        if include_buttons and ENABLE_EMAIL:
            try:
                from open_loops import get_open_loops
                loops = get_open_loops()
                urgency_order = {"high": 0, "medium": 1, "low": 2}
                loops.sort(key=lambda l: (urgency_order.get(l.urgency, 2), -l.age_days))
                loops_map = {}
                for i, loop in enumerate(loops[:15], 1):
                    loops_map[i] = loop.loop_id
                _save_digest_loops(loops_map)
            except Exception as e:
                logger.warning(f"Failed to build loop references: {e}")

            # Send pattern suggestions if any
            try:
                from interaction_tracker import detect_patterns
                patterns = detect_patterns()
                if patterns:
                    suggestion_lines = []
                    for p in patterns:
                        suggestion_lines.append(
                            f"📊 {p['description']}. Auto-deprioritize these? "
                            f"(Reply 'yes deprioritize {p['value']}' or 'no')"
                        )
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text="\n\n".join(suggestion_lines),
                    )
            except Exception as e:
                logger.warning(f"Failed to send pattern suggestions: {e}")


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Surface unhandled command/message errors to the user instead of
    failing silently. The default behavior of python-telegram-bot is to
    log the traceback and drop the user's request, which is what made
    /loops appear to "hang"."""
    err = context.error
    logger.exception("Handler raised an exception", exc_info=err)
    try:
        chat_id = None
        if isinstance(update, Update):
            if update.effective_chat:
                chat_id = update.effective_chat.id
            elif update.effective_message:
                chat_id = update.effective_message.chat_id
        if chat_id is None:
            chat_id = TELEGRAM_CHAT_ID
        msg = f"⚠️ Sorry, that request hit an error: {type(err).__name__}: {err}"
        await context.bot.send_message(chat_id=chat_id, text=msg[:4000])
    except Exception as report_err:
        logger.warning(f"Failed to surface error to user: {report_err}")


def run_bot():
    """Start the bot in long-polling mode (for interactive use)."""
    from rules import migrate_from_preferences
    migrate_from_preferences()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_error_handler(_on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_commands))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("loopcleanup", cmd_loopcleanup))
    app.add_handler(CommandHandler("memoryreview", cmd_memoryreview))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("loops", cmd_loops))
    app.add_handler(CommandHandler("availability", cmd_availability))
    app.add_handler(CommandHandler("morningavailability", cmd_morningavailability))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Bot starting in polling mode...")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
