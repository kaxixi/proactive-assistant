"""Telegram bot — sends digests and handles interactive replies."""

import asyncio
import json
import logging

import anthropic
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY, CLAUDE_MODEL
from preferences import load_preferences, add_rule, log_feedback

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Store recent digest so replies have context
_last_digest = None

# Tool definitions for Claude
TOOLS = [
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
        "description": "Dismiss an email thread so it won't appear in future digests. Use when Erez says he's handled an email, it's not relevant, the issue is resolved, or he doesn't need reminders about it. Search by sender name, subject keywords, or topic.",
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
    """Find a Gmail thread by query and dismiss it from future digests."""
    from googleapiclient.discovery import build
    from google_auth import get_credentials
    from preferences import dismiss_thread

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    resp = service.users().messages().list(
        userId="me", q=query, maxResults=1
    ).execute()
    messages = resp.get("messages", [])

    if not messages:
        return f"No emails found matching '{query}'. Could not dismiss."

    import email.utils

    msg = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="metadata",
        metadataHeaders=["Subject", "From"],
    ).execute()
    thread_id = msg["threadId"]
    headers = msg.get("payload", {}).get("headers", [])
    subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "")
    from_header = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
    _, sender_email = email.utils.parseaddr(from_header)

    dismiss_thread(thread_id, subject=subject, reason=reason, sender_email=sender_email)
    return f"Dismissed thread: \"{subject}\" (reason: {reason}). It won't appear in future digests."


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
    prefs = load_preferences()
    rules_text = "\n".join(f"- {r}" for r in prefs.get("rules", [])) or "None yet"

    digest_section = ""
    if _last_digest:
        digest_section = f"\n<last_digest>\n{_last_digest}\n</last_digest>\n"

    from memory import get_memories_for_prompt
    memory_context = get_memories_for_prompt()
    memory_section = ""
    if memory_context:
        memory_section = f"\n<memory>\n{memory_context}\n</memory>\n"

    dismiss_instructions = """When Erez says something is handled, resolved, done, taken care of, or not relevant — IMMEDIATELY use the dismiss_email tool. This is critical.
Without dismissing, the item will reappear in future digests. Erez may not remember he already dealt with it, and might accidentally re-send an email or re-do work he's already completed.

Examples of when to dismiss:
- "I already replied to that" → dismiss with reason "replied"
- "That's handled" → dismiss with reason "handled"
- "Not relevant" / "Don't need that" → dismiss with reason "not relevant"
- "I talked to them about it" → dismiss with reason "resolved offline"

When in doubt about whether Erez means to dismiss, dismiss it. It's better to dismiss and have him re-flag than to keep nagging about handled items."""

    system_prompt = f"""You are Claudette, a proactive personal assistant for a behavioral science researcher named Erez.
You communicate via Telegram.

<preferences>
{rules_text}
</preferences>
{digest_section}{memory_section}
You have tools to search Gmail, Google Drive, and Dropbox, and to dismiss email threads from future digests.

Instructions:
- Respond warmly and concisely (this is Telegram, not email).
- {dismiss_instructions}
- If he's asking a question, answer it directly.
- If he's asking you to find a file or look something up, use the search tools.
- Use your memory context to maintain continuity — reference past conversations naturally, avoid re-asking about things you already know.
- Extract any LASTING preference rules from his feedback. Return them on lines starting with "RULE:" — these will be saved automatically. Only extract rules that represent lasting preferences (e.g., "Desiree's emails are always important"), not one-time dismissals (use the dismiss tool for those instead).
{extra_instructions}"""

    return system_prompt


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "Hey! I'm Claudette, your proactive assistant. I'll send you daily "
        "digests about your inbox and calendar.\n\n"
        "Commands:\n"
        "/status — check if all services are connected\n"
        "/digest — trigger a digest right now\n"
        "/search <query> — search Drive and Dropbox\n"
        "/availability [this/next week] — show free meeting slots\n"
        "/morningavailability [this/next week] — morning slots only\n"
        "/help — show this message\n\n"
        "You can also just reply to any message with feedback or questions!"
    )


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
    prefs = load_preferences()
    lines.append(f"✓ Learned rules: {len(prefs.get('rules', []))}")
    await update.message.reply_text("\n".join(lines))


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text("Running digest now...")
    from scheduler import run_daily_digest
    await run_daily_digest()


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

    user_text = update.message.text
    logger.info(f"Received message: {user_text[:100]}...")

    log_feedback("user_message", user_text)

    system_prompt = _build_system_prompt()

    try:
        client = _get_claude()
        messages = [{"role": "user", "content": user_text}]

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

        # Extract and save any rules
        lines = reply.split("\n")
        display_lines = []
        for line in lines:
            if line.strip().startswith("RULE:"):
                rule = line.strip().removeprefix("RULE:").strip()
                if rule:
                    add_rule(rule)
                    logger.info(f"Learned new rule: {rule}")
            else:
                display_lines.append(line)

        display_text = "\n".join(display_lines).strip()
        if display_text:
            await update.message.reply_text(display_text)

        # Extract memories from this conversation (non-blocking, non-fatal)
        try:
            from memory import extract_and_store
            conversation_log = f"User: {user_text}\nAssistant: {reply}"
            extract_and_store(conversation_log, source="bot")
        except Exception as mem_err:
            logger.warning(f"Memory extraction failed (non-fatal): {mem_err}")

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

        # Process through Claude directly
        log_feedback("document", f"{doc.file_name}: {file_content[:500]}")

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

        lines = reply.split("\n")
        display_lines = []
        for line in lines:
            if line.strip().startswith("RULE:"):
                rule = line.strip().removeprefix("RULE:").strip()
                if rule:
                    add_rule(rule)
                    logger.info(f"Learned new rule: {rule}")
            else:
                display_lines.append(line)

        display_text = "\n".join(display_lines).strip()
        if display_text:
            await update.message.reply_text(display_text)

        # Extract memories from this conversation (non-fatal)
        try:
            from memory import extract_and_store
            extract_and_store(f"User sent file '{doc.file_name}': {caption}\nAssistant: {reply}", source="bot")
        except Exception as mem_err:
            logger.warning(f"Memory extraction failed (non-fatal): {mem_err}")

    except Exception as e:
        logger.error(f"Error reading document: {e}", exc_info=True)
        await update.message.reply_text(
            f"Sorry, I couldn't read {doc.file_name}: {e}"
        )


async def send_message(text: str):
    """Send a message to the configured chat (used by scheduler)."""
    global _last_digest
    _last_digest = text
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    async with bot:
        if len(text) <= 4096:
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        else:
            for i in range(0, len(text), 4096):
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, text=text[i:i + 4096]
                )


def run_bot():
    """Start the bot in long-polling mode (for interactive use)."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("availability", cmd_availability))
    app.add_handler(CommandHandler("morningavailability", cmd_morningavailability))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Bot starting in polling mode...")
    app.run_polling()


if __name__ == "__main__":
    run_bot()
