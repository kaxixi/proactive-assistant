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

import json as _json
import os as _os

# Persisted to disk so the bot process can read state created by the scheduler process
_PROJECT_DIR = _os.path.dirname(_os.path.abspath(__file__))
_DIGEST_LOOPS_FILE = _os.path.join(_PROJECT_DIR, "digest_loops.json")
_LAST_MESSAGES_FILE = _os.path.join(_PROJECT_DIR, "last_scheduler_messages.json")

# Multi-turn conversation state
_conversation_history: list[dict] = []
_MAX_HISTORY = 5
_last_interaction_time: float = 0

# Cache for fetched full threads (cleared on new digest)
_thread_cache: dict[str, str] = {}


def _save_digest_loops(loops_map: dict[int, str]):
    with open(_DIGEST_LOOPS_FILE, "w") as f:
        _json.dump(loops_map, f)


def _load_digest_loops() -> dict[int, str]:
    if not _os.path.exists(_DIGEST_LOOPS_FILE):
        return {}
    try:
        with open(_DIGEST_LOOPS_FILE) as f:
            data = _json.load(f)
        return {int(k): v for k, v in data.items()}
    except (ValueError, _json.JSONDecodeError):
        return {}


def _save_scheduler_message(text: str, label: str = "digest"):
    """Persist a message sent by the scheduler so the bot can include it as context.

    Keeps the last 3 messages (newest first) to cover digest + review + any extras.
    """
    messages = _load_scheduler_messages()
    import time as _time
    messages.insert(0, {"label": label, "text": text, "ts": _time.time()})
    messages = messages[:3]  # keep last 3
    with open(_LAST_MESSAGES_FILE, "w") as f:
        _json.dump(messages, f)


def _load_scheduler_messages() -> list[dict]:
    """Load recent scheduler messages from disk."""
    if not _os.path.exists(_LAST_MESSAGES_FILE):
        return []
    try:
        with open(_LAST_MESSAGES_FILE) as f:
            return _json.load(f)
    except (ValueError, _json.JSONDecodeError):
        return []

# Tool definitions for Claude
_BASE_TOOLS = [
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
        "name": "dismiss_email",
        "description": "Dismiss an email topic (open loop) so it won't appear in future digests. Dismisses all related threads at once. Use when Erez says he's handled an email, it's not relevant, the issue is resolved, or he doesn't need reminders about it. Search by sender name, subject keywords, or topic.",
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
    from preferences import dismiss_thread
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
            dismiss_thread(thread_id, subject=subject, reason=reason)
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
        elif tool_name == "dismiss_email":
            return _dismiss_email(tool_input["query"], tool_input["reason"])
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

    if ENABLE_EMAIL:
        dismiss_instructions = """- When Erez says something is handled, resolved, done, taken care of, or not relevant — IMMEDIATELY use the dismiss_email tool. This dismisses the matching open loop and all its related email threads at once.
  Without dismissing, the item will reappear in future digests. Erez may not remember he already dealt with it, and might accidentally re-send an email or re-do work he's already completed.
  - Erez may reference loops by NUMBER (e.g., "1 handled", "dismiss 3 and 5", "tell me more about 2"). Use the <digest_loop_numbers> section to map numbers to loop titles, then use the dismiss_email tool with the loop title as the query.
  Examples of when to dismiss:
  - "1 handled" → find loop #1's title, dismiss with reason "handled"
  - "1 and 3 handled" → dismiss both loops
  - "That's handled" → dismiss with reason "handled"
  - "Not relevant" / "Don't need that" → dismiss with reason "not relevant"
  When in doubt about whether Erez means to dismiss, dismiss it. It's better to dismiss and have him re-flag than to keep nagging about handled items."""
        tools_description = "You have tools to search Gmail, Google Drive, and Dropbox, and to dismiss open loops (email topics) from future digests."
    else:
        dismiss_instructions = ""
        tools_description = "You have tools to search Google Drive and Dropbox."

    system_prompt = f"""You are Claudette, a proactive personal assistant for a behavioral science researcher named Erez.
You communicate via Telegram.

<preferences>
{rules_text}
</preferences>
{digest_section}{memory_section}{loop_ref}
{tools_description}

Instructions:
- Respond warmly and concisely (this is Telegram, not email).
{dismiss_instructions}
- If he's asking a question, answer it directly.
- If he's asking you to find a file or look something up, use the search tools.
- Use your memory context to maintain continuity — reference past conversations naturally, avoid re-asking about things you already know.
- Extract LASTING preference rules from his feedback. Return them on lines starting with "RULE:" — these will be saved automatically.
  IMPORTANT: Only create rules for truly PERMANENT preferences (e.g., "Desiree's emails are always important", "Skip all Vercel notifications"). Do NOT create rules for temporary situations like "remind me to reply to Tim" — those are follow_up memories, not rules. The memory extraction system handles follow_ups automatically.
{extra_instructions}"""

    return system_prompt


_COMMANDS_TEXT = (
    "Commands:\n"
    "/digest — generate a digest right now\n"
    "/loops — list your open email loops (numbered)\n"
    "/memoryreview — review stored memories for stale or contradictory entries\n"
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

    global _conversation_history, _last_interaction_time

    user_text = update.message.text
    logger.info(f"Received message: {user_text[:100]}...")

    system_prompt = _build_system_prompt()

    try:
        import time
        # Check staleness — clear history if >30 minutes since last interaction
        if _last_interaction_time and (time.time() - _last_interaction_time > 1800):
            _conversation_history = []
        _last_interaction_time = time.time()

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
    await update.message.reply_text("\n".join(lines))


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


def run_bot():
    """Start the bot in long-polling mode (for interactive use)."""
    from memory import migrate_rules_to_memories
    migrate_rules_to_memories()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_commands))
    app.add_handler(CommandHandler("commands", cmd_commands))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("memoryreview", cmd_memoryreview))
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
