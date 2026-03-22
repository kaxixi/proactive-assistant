"""Gmail scanning — flags emails at risk of being dropped."""

import base64
import email.utils
import logging
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from googleapiclient.discovery import build

from google_auth import get_credentials
from preferences import load_preferences

logger = logging.getLogger(__name__)

# Thresholds (days)
REPLY_URGENCY_HIGH = 3
REPLY_URGENCY_MEDIUM = 7
FOLLOWUP_THRESHOLD = 5

# Automated/noise senders to always skip (pattern matching)
AUTOMATED_SENDER_PATTERNS = [
    r"noreply@",
    r"no-reply@",
    r"notifications?@",
    r"mailer-daemon@",
    r".*@.*\.vercel\.com",
    r".*@vercel\.com",
    r".*@github\.com",
    r".*@googlegroups\.com",
]

# Subject patterns that indicate automated/newsletter emails
NEWSLETTER_PATTERNS = [
    r"^(re:\s*)?unsubscribe",
    r"weekly digest",
    r"daily digest",
    r"newsletter",
    r"your .* summary",
    r"notification from",
]


@dataclass
class FlaggedEmail:
    subject: str
    sender: str
    sender_name: str
    date: datetime
    age_days: int
    thread_id: str
    message_id: str
    reason: str  # "unreplied" or "needs_followup"
    urgency: str  # "high", "medium", "low"
    snippet: str = ""
    labels: list = field(default_factory=list)
    is_newsletter: bool = False


def _get_gmail_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds)


def _parse_date(headers: list) -> datetime:
    for h in headers:
        if h["name"].lower() == "date":
            parsed = email.utils.parsedate_to_datetime(h["value"])
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.now(timezone.utc)


def _get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _extract_email_address(from_header: str) -> str:
    _, addr = email.utils.parseaddr(from_header)
    return addr.lower()


def _extract_name(from_header: str) -> str:
    name, _ = email.utils.parseaddr(from_header)
    return name or from_header


def _is_automated_sender(email_addr: str, never_flag: list) -> bool:
    """Check if sender is automated/noise."""
    if email_addr in never_flag:
        return True
    for pattern in AUTOMATED_SENDER_PATTERNS:
        if re.match(pattern, email_addr, re.IGNORECASE):
            return True
    return False


def _is_newsletter(subject: str, headers: list) -> bool:
    """Check if email looks like a newsletter/subscription."""
    subj_lower = subject.lower()
    for pattern in NEWSLETTER_PATTERNS:
        if re.search(pattern, subj_lower):
            return True
    # Check for List-Unsubscribe header (strong newsletter signal)
    if _get_header(headers, "List-Unsubscribe"):
        return True
    return False


def _extract_body_preview(payload: dict, max_chars: int = 500) -> str:
    """Extract plain text body preview from a full-format Gmail message payload."""
    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return ""

    # Try to find text/plain part
    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return _decode(data)[:max_chars]
            # Handle nested multipart
            if "parts" in part:
                for subpart in part["parts"]:
                    if subpart.get("mimeType") == "text/plain":
                        data = subpart.get("body", {}).get("data", "")
                        if data:
                            return _decode(data)[:max_chars]

    # Single-part message
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return _decode(body_data)[:max_chars]

    return ""


def fetch_full_thread(thread_id: str, max_chars_per_message: int = 2000) -> str:
    """Fetch all messages in a Gmail thread and format them chronologically.

    Returns a formatted string with each message's sender, date, and body.
    Used for "tell me more" deep dives on open loops.
    """
    service = _get_gmail_service()
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()

    messages = thread.get("messages", [])
    formatted_parts = []

    for msg in messages:
        headers = msg.get("payload", {}).get("headers", [])
        sender = _get_header(headers, "From")
        date = _get_header(headers, "Date")
        subject = _get_header(headers, "Subject")

        body = _extract_body_preview(msg.get("payload", {}), max_chars=max_chars_per_message)

        # Extract display name from sender
        sender_display = sender.split("<")[0].strip().strip('"') if "<" in sender else sender

        formatted_parts.append(
            f"--- {sender_display} ({date}) ---\n"
            f"Subject: {subject}\n"
            f"{body}\n"
        )

    return "\n".join(formatted_parts)


def scan_inbox(my_email: str = None, days_back: int = 14) -> list[FlaggedEmail]:
    """Scan inbox for emails that need attention.

    Returns a list of FlaggedEmail objects sorted by urgency.
    """
    service = _get_gmail_service()
    now = datetime.now(timezone.utc)
    prefs = load_preferences()
    never_flag = prefs.get("senders_never_flag", [])
    always_flag = prefs.get("senders_always_flag", [])

    if not my_email:
        profile = service.users().getProfile(userId="me").execute()
        my_email = profile["emailAddress"].lower()

    after_date = (now - timedelta(days=days_back)).strftime("%Y/%m/%d")
    query = f"in:inbox after:{after_date}"

    threads = []
    page_token = None
    while True:
        resp = service.users().threads().list(
            userId="me", q=query, pageToken=page_token, maxResults=100
        ).execute()
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"Found {len(threads)} inbox threads from last {days_back} days")

    flagged = []
    newsletters = []

    # Batch fetch thread details (much faster than one-by-one)
    thread_details = []
    batch_size = 20

    for i in range(0, len(threads), batch_size):
        batch = service.new_batch_http_request()
        batch_results = {}

        def _make_callback(tid):
            def callback(request_id, response, exception):
                if exception is None:
                    batch_results[tid] = response
                else:
                    logger.warning(f"Batch fetch failed for thread {tid}: {exception}")
            return callback

        for thread_meta in threads[i:i + batch_size]:
            tid = thread_meta["id"]
            batch.add(
                service.users().threads().get(
                    userId="me", id=tid, format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date", "List-Unsubscribe"],
                ),
                callback=_make_callback(tid),
            )

        batch.execute()
        thread_details.extend(batch_results.values())

    logger.info(f"Fetched {len(thread_details)} thread details via batch API")

    for thread in thread_details:
        messages = thread.get("messages", [])
        if not messages:
            continue

        first_msg = messages[0]
        last_msg = messages[-1]

        first_headers = first_msg.get("payload", {}).get("headers", [])
        last_headers = last_msg.get("payload", {}).get("headers", [])

        subject = _get_header(first_headers, "Subject") or "(no subject)"
        last_from = _get_header(last_headers, "From")
        last_sender_email = _extract_email_address(last_from)
        last_date = _parse_date(last_headers)
        age = (now - last_date).days

        first_from = _get_header(first_headers, "From")
        original_sender_email = _extract_email_address(first_from)
        original_sender_name = _extract_name(first_from)

        labels = last_msg.get("labelIds", [])

        # Skip automated senders (unless in always-flag list)
        if original_sender_email not in always_flag:
            if _is_automated_sender(last_sender_email, never_flag):
                continue

        # Detect newsletters
        is_newsletter = _is_newsletter(subject, last_headers)
        if is_newsletter and original_sender_email not in always_flag:
            newsletters.append(FlaggedEmail(
                subject=subject,
                sender=original_sender_email,
                sender_name=original_sender_name,
                date=last_date,
                age_days=age,
                thread_id=thread["id"],
                message_id=last_msg["id"],
                reason="newsletter",
                urgency="low",
                snippet=last_msg.get("snippet", "")[:200],
                labels=labels,
                is_newsletter=True,
            ))
            continue

        # --- Heuristic 1: Unreplied emails where someone else spoke last ---
        if last_sender_email != my_email:
            if age >= REPLY_URGENCY_HIGH:
                urgency = "high"
            elif age >= REPLY_URGENCY_MEDIUM:
                urgency = "medium"
            else:
                urgency = "low"

            # Skip low urgency unless important
            if urgency == "low" and "IMPORTANT" not in labels:
                if original_sender_email not in always_flag:
                    continue

            flagged.append(FlaggedEmail(
                subject=subject,
                sender=original_sender_email,
                sender_name=original_sender_name,
                date=last_date,
                age_days=age,
                thread_id=thread["id"],
                message_id=last_msg["id"],
                reason="unreplied",
                urgency=urgency,
                snippet=last_msg.get("snippet", "")[:200],
                labels=labels,
            ))

        # --- Heuristic 2: I replied last — might need to follow up ---
        elif last_sender_email == my_email and len(messages) > 1:
            if age >= FOLLOWUP_THRESHOLD:
                flagged.append(FlaggedEmail(
                    subject=subject,
                    sender=original_sender_email,
                    sender_name=original_sender_name,
                    date=last_date,
                    age_days=age,
                    thread_id=thread["id"],
                    message_id=last_msg["id"],
                    reason="needs_followup",
                    urgency="medium" if age >= REPLY_URGENCY_MEDIUM else "low",
                    snippet=last_msg.get("snippet", "")[:200],
                    labels=labels,
                ))

    # Sort: high urgency first, then by age descending
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    flagged.sort(key=lambda e: (urgency_order[e.urgency], -e.age_days))

    # Second pass: fetch body previews for flagged emails (format=full, last message only)
    if flagged:
        logger.info(f"Fetching body previews for {len(flagged)} flagged emails...")
        msg_to_email = {}  # message_id → FlaggedEmail
        for fe in flagged:
            msg_to_email[fe.message_id] = fe

        for i in range(0, len(flagged), batch_size):
            batch = service.new_batch_http_request()
            batch_results = {}

            def _make_body_callback(mid):
                def callback(request_id, response, exception):
                    if exception is None:
                        batch_results[mid] = response
                    else:
                        logger.warning(f"Body fetch failed for {mid}: {exception}")
                return callback

            for fe in flagged[i:i + batch_size]:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=fe.message_id, format="full",
                    ),
                    callback=_make_body_callback(fe.message_id),
                )

            batch.execute()

            for mid, msg_data in batch_results.items():
                payload = msg_data.get("payload", {})
                body = _extract_body_preview(payload)
                if body and mid in msg_to_email:
                    # Replace the short snippet with the richer body preview
                    msg_to_email[mid].snippet = body

        logger.info("Body previews fetched")

    logger.info(f"Flagged {len(flagged)} actionable emails, {len(newsletters)} newsletters")
    return flagged
