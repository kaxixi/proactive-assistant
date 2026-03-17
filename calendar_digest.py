"""Google Calendar digest — today's and tomorrow's meetings with prep flags."""

import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from googleapiclient.discovery import build

from google_auth import get_credentials

logger = logging.getLogger(__name__)

# Keywords that suggest a meeting needs prep
PREP_KEYWORDS = [
    "review", "presentation", "demo", "interview", "defense",
    "workshop", "committee", "proposal", "grant", "submission", "deadline",
]

# Keywords that indicate passive/listening events (no prep needed)
PASSIVE_KEYWORDS = [
    "seminar", "talk", "lecture", "culture lab", "colloquium",
    "brown bag", "lunch talk",
]


@dataclass
class Meeting:
    summary: str
    start: datetime
    end: datetime
    location: str
    attendees: list
    description: str
    needs_prep: bool
    prep_reason: str
    is_tomorrow: bool
    is_recurring: bool = False


def _get_calendar_service():
    creds = get_credentials()
    return build("calendar", "v3", credentials=creds)


def get_user_timezone() -> str:
    """Fetch the user's timezone from Google Calendar settings."""
    service = _get_calendar_service()
    setting = service.settings().get(setting="timezone").execute()
    tz = setting["value"]
    logger.info(f"User calendar timezone: {tz}")
    return tz


def _check_needs_prep(summary: str, description: str, attendees: list) -> tuple[bool, str]:
    """Heuristic: does this meeting likely need preparation?"""
    text = f"{summary} {description}".lower()

    # Check passive events first — these never need prep
    for kw in PASSIVE_KEYWORDS:
        if kw in text:
            return False, ""

    for kw in PREP_KEYWORDS:
        if kw in text:
            return True, f"contains '{kw}'"

    if len(attendees) > 5:
        return True, f"{len(attendees)} attendees"

    return False, ""


def get_meetings_for_range(days: int = 2) -> list[Meeting]:
    """Fetch meetings for a given number of days starting from today in user's timezone."""
    from zoneinfo import ZoneInfo
    service = _get_calendar_service()
    tz_name = get_user_timezone()
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    today_local = local_now.date()

    # Use local midnight as the range start
    today_start = datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz)
    range_end = today_start + timedelta(days=days)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=today_start.isoformat(),
        timeMax=range_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = events_result.get("items", [])
    logger.info(f"Found {len(events)} events for next {days} days")

    meetings = []
    for event in events:
        # Handle all-day events vs timed events
        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        end_raw = event["end"].get("dateTime", event["end"].get("date"))

        if "T" in start_raw:
            start = datetime.fromisoformat(start_raw)
            end = datetime.fromisoformat(end_raw)
        else:
            start = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=tz)
            end = datetime.strptime(end_raw, "%Y-%m-%d").replace(tzinfo=tz)

        summary = event.get("summary", "(no title)")
        description = event.get("description", "")
        attendees = event.get("attendees", [])
        location = event.get("location", "")
        is_recurring = "recurringEventId" in event

        needs_prep, prep_reason = _check_needs_prep(summary, description, attendees)

        # Non-recurring meetings get extra attention
        if not is_recurring and not needs_prep:
            needs_prep = False  # don't auto-flag, but we'll pass the info to Claude
            prep_reason = ""

        is_tomorrow = start.date() > today_local

        meetings.append(Meeting(
            summary=summary,
            start=start,
            end=end,
            location=location,
            attendees=[a.get("email", "") for a in attendees],
            description=description[:500],
            needs_prep=needs_prep,
            prep_reason=prep_reason,
            is_tomorrow=is_tomorrow,
            is_recurring=is_recurring,
        ))

    return meetings


def get_upcoming_meetings() -> list[Meeting]:
    """Fetch today's and tomorrow's meetings (weekday digest)."""
    return get_meetings_for_range(days=2)


def format_calendar_digest(meetings: list[Meeting]) -> str:
    """Format meetings into a readable daily digest."""
    if not meetings:
        return "📅 No meetings today or tomorrow — clear schedule."

    today = [m for m in meetings if not m.is_tomorrow]
    tomorrow = [m for m in meetings if m.is_tomorrow]

    lines = []

    if today:
        lines.append(f"📅 **Today — {len(today)} meeting{'s' if len(today) != 1 else ''}:**\n")
        for m in today:
            time_str = m.start.strftime("%-I:%M %p") if "T" in m.start.isoformat() else "All day"
            prep_flag = " ⚡ PREP" if m.needs_prep else ""
            lines.append(f"  • **{time_str}** — {m.summary}{prep_flag}")
            if m.location:
                lines.append(f"    📍 {m.location}")
            if m.needs_prep:
                lines.append(f"    _Needs prep: {m.prep_reason}_")
        lines.append("")

    if tomorrow:
        lines.append(f"📅 **Tomorrow — {len(tomorrow)} meeting{'s' if len(tomorrow) != 1 else ''}:**\n")
        for m in tomorrow:
            time_str = m.start.strftime("%-I:%M %p") if "T" in m.start.isoformat() else "All day"
            prep_flag = " ⚡ PREP" if m.needs_prep else ""
            lines.append(f"  • **{time_str}** — {m.summary}{prep_flag}")
            if m.location:
                lines.append(f"    📍 {m.location}")
            if m.needs_prep:
                lines.append(f"    _Needs prep: {m.prep_reason}_")

    return "\n".join(lines)
