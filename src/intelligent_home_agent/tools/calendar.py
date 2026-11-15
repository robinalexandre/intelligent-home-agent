"""The user's calendars over CalDAV (iCloud, Fastmail, Nextcloud...): read, and add events.

New events go to the calendar named in the settings, else to the one the server gives as
default (``schedule-default-calendar-URL``, RFC 6638), else to the first that takes
events. Adding one is a write: after reading an email it waits for the user's go.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import caldav
import icalendar
from caldav.elements.base import BaseElement
from caldav.elements.cdav import ns

from intelligent_home_agent.i18n import strings
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


class _DefaultCalendarURL(BaseElement):
    tag = ns("C", "schedule-default-calendar-URL")


def default_calendar_path(found: Any) -> str:
    """The default calendar's path from the server's answer: an XML element with an
    href, or (seen on iCloud) the path as its own text."""
    if found is None:
        return ""
    is_element = not isinstance(found, str) and hasattr(found, "find")
    href = found.find(".//{DAV:}href") if is_element else None
    if href is not None:
        text = href.text
    else:
        text = found.text if hasattr(found, "text") else str(found)
    match = re.search(r"(/[^<>\s]+)", text or "")
    return match.group(1).rstrip("/") if match else ""


def event_ical(
    title: str,
    start: datetime | date,
    end: datetime | date,
    location: str | None,
    notes: str | None,
) -> bytes:
    event = icalendar.Event()
    event.add("uid", f"{uuid.uuid4()}@intelligent-home-agent")
    event.add("dtstamp", datetime.now(ZoneInfo("UTC")))
    event.add("summary", title)
    event.add("dtstart", start)
    event.add("dtend", end)
    if location:
        event.add("location", location)
    if notes:
        event.add("description", notes)
    cal = icalendar.Calendar()
    cal.add("prodid", "-//intelligent-home-agent//EN")
    cal.add("version", "2.0")
    cal.add_component(event)
    return cal.to_ical()


class CalDavCalendar:
    """Blocking ``caldav`` calls, run in a worker thread."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar_names: list[str],
        default_calendar: str | None = None,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._names = {name.casefold() for name in calendar_names}
        self._default_name = (default_calendar or "").strip()
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

    def _default(self) -> tuple[Any, str]:
        client = caldav.DAVClient(
            url=self._url, username=self._username, password=self._password, timeout=15
        )
        principal = client.principal()
        calendars = principal.calendars()
        if self._default_name:
            names = [cal.get_display_name() or "" for cal in calendars]
            for cal, name in zip(calendars, names, strict=True):
                if name.casefold() == self._default_name.casefold():
                    return cal, name
            raise ToolError(f"No calendar named {self._default_name}. Calendars: {names}")
        try:
            found = principal.schedule_inbox().get_property(
                _DefaultCalendarURL(), parse_props=False
            )
            default = default_calendar_path(found)
        except Exception:
            default = ""
        for cal in calendars:
            if default and str(cal.url).rstrip("/").endswith(default):
                return cal, cal.get_display_name() or ""
        # No default given (many servers have none): the first one that holds events,
        # among the calendars the settings keep when they name some.
        kept = [c for c in calendars if (c.get_display_name() or "").casefold() in self._names]
        for cal in kept or calendars:
            if "VEVENT" in (cal.get_supported_components() or ["VEVENT"]):
                return cal, cal.get_display_name() or ""
        raise ToolError("No calendar takes events.")

    def _add(self, ical: bytes) -> str:
        cal, name = self._default()
        cal.add_event(ical.decode())
        return name

    async def add_event(self, ical: bytes) -> str:
        """Adds it to the default calendar and says which one that is."""
        try:
            return await asyncio.to_thread(self._add, ical)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Calendar unavailable: {type(exc).__name__}") from exc

    async def default_name(self) -> str:
        try:
            return (await asyncio.to_thread(self._default))[1]
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Calendar unavailable: {type(exc).__name__}") from exc


def place_key(latitude: float, longitude: float) -> str:
    return f"{float(latitude)},{float(longitude)}"


def _sort_key(event: CalendarEvent, tz: ZoneInfo) -> datetime:
    if isinstance(event.start, datetime):
        return event.start.astimezone(tz)
    return datetime.combine(event.start, time.min, tz)


def format_when(value: datetime | date | None, tz: ZoneInfo) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        local = value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
        return local.isoformat(timespec="minutes")
    return value.isoformat()


def _when(value: Any, tz: ZoneInfo, all_day: bool) -> datetime | date:
    text = str(value or "").strip()
    try:
        if all_day:
            return date.fromisoformat(text[:10])
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError('Times are "YYYY-MM-DD HH:MM", days "YYYY-MM-DD".') from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def build_calendar_write_tools(
    calendar: CalDavCalendar, timezone: str, language: str = "en"
) -> list[Tool]:
    tz = ZoneInfo(timezone)
    s = strings(language)

    def event_times(args: dict[str, Any]) -> tuple[datetime | date, datetime | date]:
        all_day = bool(args.get("all_day"))
        start = _when(args.get("start"), tz, all_day)
        if args.get("end"):
            end = _when(args["end"], tz, all_day)
        else:
            end = start + timedelta(days=1) if all_day else start + timedelta(hours=1)  # type: ignore[operator]
        if end <= start:  # type: ignore[operator]
            raise ToolError("The end must come after the start.")
        return start, end

    async def create_calendar_event(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        title = " ".join(str(args.get("title") or "").split())
        if not title:
            raise ToolError("title is required.")
        start, end = event_times(args)
        ical = event_ical(title, start, end, args.get("location"), args.get("notes"))
        name = await calendar.add_event(ical)
        return {"added_to": name, "title": title, "start": format_when(start, tz)}

    async def describe(args: dict[str, Any]) -> str:
        start, end = event_times(args)
        where = s.calendar_where.format(location=args["location"]) if args.get("location") else ""
        notes = s.calendar_notes.format(notes=args["notes"]) if args.get("notes") else ""
        return s.calendar_add_summary.format(
            title=args.get("title"),
            start=format_when(start, tz),
            end=format_when(end, tz),
            where=where,
            notes=notes,
        )

    return [
        Tool(
            name="create_calendar_event",
            description=(
                "Add an event to the user's default calendar (it says which one). "
                "Without end: one hour, or one day when all_day. After reading an email or "
                "an invitation, it waits for the user's go."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": '"YYYY-MM-DD HH:MM", local time'},
                    "end": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["title", "start"],
            },
            handler=create_calendar_event,
            needs_clean_turn=True,
            proposable=True,
            describe=describe,
        )
    ]


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
                    "start": format_when(e.start, tz),
                    "end": format_when(e.end, tz),
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
