"""Availability computation — generates formatted free slots from Google Calendar."""

import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from google_auth import get_credentials
from calendar_digest import get_user_timezone

logger = logging.getLogger(__name__)

# Availability windows in minutes from midnight
MORNING_WINDOW = (9 * 60 + 15, 11 * 60 + 30)   # 9:15 AM – 11:30 AM
AFTERNOON_WINDOW = (14 * 60 + 30, 17 * 60)       # 2:30 PM – 5:00 PM

BUFFER_MINUTES = 45

VIRTUAL_KEYWORDS = {"zoom", "meet", "teams", "webex", "virtual", "online", "remote", "http"}


def _is_virtual(location: str) -> bool:
    """Empty location or video-call keywords = virtual."""
    if not location or not location.strip():
        return True
    loc_lower = location.lower()
    return any(kw in loc_lower for kw in VIRTUAL_KEYWORDS)


def _parse_week(args: str) -> tuple[date, date, str]:
    """Parse week specification from user input.
    Returns (monday, friday, label).
    Supports: 'this week', 'next week', 'week of May 12', 'wk of Mar 3', etc.
    """
    import re
    tz_name = get_user_timezone()
    today = datetime.now(ZoneInfo(tz_name)).date()

    args_lower = args.lower().strip()

    # Try to find a date reference like "May 12", "Mar 3", "3/15", "2026-05-12"
    # Month name + day pattern
    month_names = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
        "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }

    # "May 12" or "Mar 3"
    match = re.search(r'(\w+)\s+(\d{1,2})', args_lower)
    if match:
        month_str, day_str = match.group(1), match.group(2)
        if month_str in month_names:
            month = month_names[month_str]
            day = int(day_str)
            year = today.year
            target = date(year, month, day)
            # If the date is in the past by more than 6 months, assume next year
            if target < today - timedelta(days=180):
                target = date(year + 1, month, day)
            # Snap weekends to the nearest workweek: Sat→prior Mon, Sun→next Mon
            if target.weekday() == 5:  # Saturday
                target -= timedelta(days=5)  # back to Monday
            elif target.weekday() == 6:  # Sunday
                target += timedelta(days=1)  # forward to Monday
            monday = target - timedelta(days=target.weekday())
            friday = monday + timedelta(days=4)
            label = f"Week of {monday.strftime('%b %-d')}"
            return monday, friday, label

    # "3/15" or "5/12"
    match = re.search(r'(\d{1,2})/(\d{1,2})', args_lower)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        year = today.year
        target = date(year, month, day)
        if target < today - timedelta(days=180):
            target = date(year + 1, month, day)
        if target.weekday() >= 5:
            target += timedelta(days=(7 - target.weekday()))
        monday = target - timedelta(days=target.weekday())
        friday = monday + timedelta(days=4)
        label = f"Week of {monday.strftime('%b %-d')}"
        return monday, friday, label

    # "next week"
    if "next" in args_lower:
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        friday = monday + timedelta(days=4)
        return monday, friday, "Next Week"

    # Default: this week
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday, "This Week"


def _fetch_events(start_date: date, end_date: date, tz_name: str) -> list[dict]:
    """Fetch raw calendar events for a date range."""
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)

    tz = ZoneInfo(tz_name)
    time_min = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    time_max = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return events_result.get("items", [])


def _event_to_local_minutes(event: dict, day: date, tz: ZoneInfo) -> tuple[int, int, bool] | None:
    """Convert an event to (start_min, end_min, is_virtual) for a given day.
    Returns None if the event doesn't fall on this day."""
    start_raw = event["start"].get("dateTime", event["start"].get("date"))
    end_raw = event["end"].get("dateTime", event["end"].get("date"))

    # All-day events — ignore (e.g., pet care, birthdays)
    if "T" not in start_raw:
        return None

    start_dt = datetime.fromisoformat(start_raw).astimezone(tz)
    end_dt = datetime.fromisoformat(end_raw).astimezone(tz)

    # Check if event falls on this day
    if start_dt.date() != day and end_dt.date() != day:
        return None

    # Clip to this day
    start_min = start_dt.hour * 60 + start_dt.minute if start_dt.date() == day else 0
    end_min = end_dt.hour * 60 + end_dt.minute if end_dt.date() == day else 24 * 60

    location = event.get("location", "")
    virtual = _is_virtual(location)

    return (start_min, end_min, virtual)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping intervals."""
    if not intervals:
        return []
    sorted_ivs = sorted(intervals)
    merged = [sorted_ivs[0]]
    for start, end in sorted_ivs[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract_intervals(
    free: list[tuple[int, int]], blocked: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Subtract blocked intervals from free intervals."""
    result = []
    for fs, fe in free:
        remaining = [(fs, fe)]
        for bs, be in blocked:
            new_remaining = []
            for rs, re in remaining:
                if be <= rs or bs >= re:
                    new_remaining.append((rs, re))
                else:
                    if rs < bs:
                        new_remaining.append((rs, bs))
                    if be < re:
                        new_remaining.append((be, re))
            remaining = new_remaining
        result.extend(remaining)
    return result


def _format_time(minutes: int) -> str:
    """Convert minutes-from-midnight to '9:15 AM' format."""
    h = minutes // 60
    m = minutes % 60
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    if m == 0:
        return f"{h12} {period}"
    return f"{h12}:{m:02d} {period}"


def compute_availability(args: str = "", morning_only: bool = False) -> str:
    """Compute and format availability for a week.
    args: user input like 'this week', 'next week', 'wk of May 12', etc.
    """
    monday, friday, week_label = _parse_week(args)
    tz_name = get_user_timezone()
    tz = ZoneInfo(tz_name)

    events = _fetch_events(monday, friday, tz_name)
    today = datetime.now(tz).date()

    windows = [MORNING_WINDOW]
    if not morning_only:
        windows.append(AFTERNOON_WINDOW)

    lines = [f"📅 <b>Availability — {week_label}</b> (Eastern Time, <b>bold</b> = preferred)\n"]

    for offset in range(5):  # Mon–Fri
        day = monday + timedelta(days=offset)

        # Skip past days, and skip today if past the last window
        if day < today:
            continue
        if day == today:
            now_minutes = datetime.now(tz).hour * 60 + datetime.now(tz).minute
            last_window_end = windows[-1][1]
            if now_minutes >= last_window_end:
                continue

        day_events = []
        virtual_boundaries = set()

        for event in events:
            result = _event_to_local_minutes(event, day, tz)
            if result is None:
                continue
            start_min, end_min, is_virtual = result

            if is_virtual:
                # Virtual: block just the meeting time, track boundaries
                day_events.append((start_min, end_min))
                virtual_boundaries.add(start_min)
                virtual_boundaries.add(end_min)
            else:
                # Non-virtual: add 45 min buffer on each side
                day_events.append((start_min - BUFFER_MINUTES, end_min + BUFFER_MINUTES))

        blocked = _merge_intervals(day_events)

        day_label = day.strftime("%A, %b %-d")
        day_slots = []

        for window in windows:
            free = _subtract_intervals([window], blocked)
            # Filter out tiny slots (< 15 min)
            free = [(s, e) for s, e in free if e - s >= 15]

            for slot_start, slot_end in free:
                time_str = f"{_format_time(slot_start)}–{_format_time(slot_end)}"
                # Bold if adjacent to a virtual meeting
                if slot_start in virtual_boundaries or slot_end in virtual_boundaries:
                    day_slots.append(f"  <b>{time_str}</b>")
                else:
                    day_slots.append(f"  {time_str}")

        if day_slots:
            lines.append(f"<b>{day_label}</b>")
            lines.extend(day_slots)
            lines.append("")
        else:
            lines.append(f"<b>{day_label}</b>")
            lines.append("  No availability")
            lines.append("")

    return "\n".join(lines).strip()
