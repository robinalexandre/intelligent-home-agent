"""Read-only access to the user's calendars over CalDAV (iCloud, Fastmail, Nextcloud...)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import caldav

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_EVENTS = 50
# Apple Calendar stores the picked place as e.g. "geo:48.8584,2.2945" in this property.
_APPLE_GEO_RE = re.compile(r"geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: datetime | date
    end: datetime | date | None
    calendar: str
    location: str | None = None
    coordinates: tuple[float, float] | None = None

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


class CalendarSource(Protocol):
    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


def event_from_component(component: Any, calendar: str) -> CalendarEvent:
    """Build an event from an ``icalendar`` VEVENT component."""
    start = component.decoded("dtstart")
    if "dtend" in component:
        end = component.decoded("dtend")
    elif "duration" in component:
        end = start + component.decoded("duration")
    else:
        end = None
    location = component.get("location")
    geo = _APPLE_GEO_RE.search(str(component.get("x-apple-structured-location", "")))
    return CalendarEvent(
        title=str(component.get("summary", "")).strip() or "(untitled)",
        start=start,
        end=end,
        calendar=calendar,
        location=str(location).strip() if location else None,
        coordinates=(float(geo.group(1)), float(geo.group(2))) if geo else None,
    )


class CalDavCalendar:
    """Blocking ``caldav`` calls, run in a worker thread."""

    def __init__(self, url: str, username: str, password: str, calendar_names: list[str]) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._names = {name.casefold() for name in calendar_names}
        self._calendars: list[tuple[Any, str]] | None = None

    def _load_calendars(self) -> list[tuple[Any, str]]:
        client = caldav.DAVClient(
            url=self._url, username=self._username, password=self._password, timeout=15
        )
        calendars = [(c, c.get_display_name() or "") for c in client.principal().calendars()]
        if self._names:
            calendars = [(c, name) for c, name in calendars if name.casefold() in self._names]
        return calendars

    def _fetch(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        if self._calendars is None:
            self._calendars = self._load_calendars()
        events = []
        for cal, name in self._calendars:
            for item in cal.search(event=True, start=start, end=end, expand=True):
                events.append(event_from_component(item.component, name))
        return events

    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        try:
            return await asyncio.to_thread(self._fetch, start, end)
        except Exception as exc:
            self._calendars = None
            raise ToolError(f"Calendar unavailable: {type(exc).__name__}") from exc


def place_key(latitude: float, longitude: float) -> str:
    return f"{float(latitude)},{float(longitude)}"


def _sort_key(event: CalendarEvent, tz: ZoneInfo) -> datetime:
    if isinstance(event.start, datetime):
        return event.start.astimezone(tz)
    return datetime.combine(event.start, time.min, tz)


def _format(value: datetime | date | None, tz: ZoneInfo) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        local = value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
        return local.isoformat(timespec="minutes")
    return value.isoformat()


def build_calendar_tools(source: CalendarSource, timezone: str) -> list[Tool]:
    tz = ZoneInfo(timezone)

    async def get_calendar_events(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        try:
            first_day = date.fromisoformat(args["date"]) if args.get("date") else None
        except ValueError as exc:
            raise ToolError("date must be YYYY-MM-DD") from exc
        first_day = first_day or datetime.now(tz).date()
        days = min(max(int(args.get("days", 1)), 1), 14)
        start = datetime.combine(first_day, time.min, tz)
        end = start + timedelta(days=days)

        events = sorted(await source.events(start, end), key=lambda e: _sort_key(e, tz))
        for e in events[:MAX_EVENTS]:
            if e.location:
                ctx.calendar_places.add(e.location)
            if e.coordinates:
                ctx.calendar_places.add(place_key(*e.coordinates))
        return {
            "from": first_day.isoformat(),
            "days": days,
            "count": len(events),
            "events": [
                {
                    "title": e.title,
                    "start": _format(e.start, tz),
                    "end": _format(e.end, tz),
                    "all_day": e.all_day,
                    "location": e.location,
                    "coordinates": list(e.coordinates) if e.coordinates else None,
                    "calendar": e.calendar,
                }
                for e in events[:MAX_EVENTS]
            ],
        }

    return [
        Tool(
            name="get_calendar_events",
            description=(
                "The user's calendar events from a given day, for 1 to 14 days. Defaults to today."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "First day, YYYY-MM-DD."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 14},
                },
            },
            handler=get_calendar_events,
            taints=True,
        )
    ]
