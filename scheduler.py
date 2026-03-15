"""Entry point for scheduled daily runs and manual triggers."""

import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from email_monitor import scan_inbox
from calendar_digest import get_upcoming_meetings, get_meetings_for_range, get_user_timezone
from analyzer import generate_daily_digest
from preferences import load_preferences, get_dismissed_context
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

        # 3. Generate digest with Claude
        logger.info("Generating digest with Claude...")
        digest = generate_daily_digest(
            flagged_emails, meetings, prefs, priorities,
            memories_context, dismissed_context, digest_type,
        )

        # 4. Send via Telegram
        await send_message(digest)
        logger.info("Daily digest sent successfully")

        # 5. Extract memories from the digest we just sent
        try:
            extract_and_store(f"Daily digest sent to Erez:\n{digest}", source="digest")
        except Exception as e:
            logger.warning(f"Memory extraction from digest failed (non-fatal): {e}")

        # 6. Run memory compaction if needed
        try:
            compact_memories()
        except Exception as e:
            logger.warning(f"Memory compaction failed (non-fatal): {e}")

        # 7. Sunday: memory review + housekeeping
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
