from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest

from intelligent_home_agent.main import with_waiting
from intelligent_home_agent.tools import (
    CalDavCalendar,
    ToolContext,
    ToolError,
    ToolRegistry,
    build_calendar_tools,
    build_calendar_write_tools,
)
from intelligent_home_agent.tools.calendar import (
    CalendarEvent,
    default_calendar_path,
    event_from_component,
)
from intelligent_home_agent.tools.proposals import build_proposal_tools

PARIS = ZoneInfo("Europe/Paris")


def vevent(**props):
    component = icalendar.Event()
    for key, value in props.items():
        component.add(key, value)
    return component


def test_timed_event_from_component():
    event = event_from_component(
        vevent(
            summary="Dentist",
            dtstart=datetime(2026, 10, 14, 9, 30, tzinfo=PARIS),
            duration=timedelta(minutes=45),
            location="1 Example Street",
        ),
        "Personal",
    )
    assert event.title == "Dentist"
    assert event.end == datetime(2026, 10, 14, 10, 15, tzinfo=PARIS)
    assert event.location == "1 Example Street"
    assert not event.all_day


def test_apple_structured_location_gives_coordinates():
    component = vevent(summary="Client", dtstart=datetime(2026, 10, 14, 9, 0, tzinfo=PARIS))
    component.add(
        "x-apple-structured-location",
        "geo:48.8584,2.2945",
        parameters={"VALUE": "URI", "X-TITLE": "Eiffel Tower"},
    )
    assert event_from_component(component, "Personal").coordinates == (48.8584, 2.2945)
    plain = vevent(summary="x", dtstart=datetime(2026, 10, 14, 9, 0, tzinfo=PARIS))
    assert event_from_component(plain, "Personal").coordinates is None


def test_all_day_event_without_title():
    event = event_from_component(vevent(dtstart=date(2026, 10, 14)), "Personal")
    assert event.title == "(untitled)"
    assert event.all_day
    assert event.end is None


class FakeCalendar:
    def __init__(self, events):
        self._events = events
        self.ranges = []

    async def events(self, start, end):
        self.ranges.append((start, end))
        return self._events


async def test_events_are_sorted_and_all_day_first(ctx):
    source = FakeCalendar(
        [
            CalendarEvent("Meeting", datetime(2026, 10, 14, 14, 0, tzinfo=PARIS), None, "Work"),
            CalendarEvent("Jo's birthday", date(2026, 10, 14), date(2026, 10, 15), "Personal"),
            CalendarEvent(
                "Client call", datetime(2026, 10, 14, 7, 0, tzinfo=ZoneInfo("UTC")), None, "Work"
            ),
        ]
    )
    (tool,) = build_calendar_tools(source, "Europe/Paris")

    result = await tool.handler({"date": "2026-10-14", "days": 1}, ctx)

    assert [e["title"] for e in result["events"]] == ["Jo's birthday", "Client call", "Meeting"]
    assert result["events"][1]["start"] == "2026-10-14T09:00+02:00"
    assert result["events"][0]["all_day"] is True
    assert ctx.calendar_places == set()
    start, end = source.ranges[0]
    assert start == datetime(2026, 10, 14, tzinfo=PARIS)
    assert end - start == timedelta(days=1)


async def test_event_places_are_remembered_for_travel_time(ctx):
    event = CalendarEvent(
        "Client",
        datetime(2026, 10, 14, 9, 0, tzinfo=PARIS),
        None,
        "Work",
        location="1 Example Street, Springfield",
        coordinates=(48.8584, 2.2945),
    )
    (tool,) = build_calendar_tools(FakeCalendar([event]), "Europe/Paris")
    await tool.handler({"date": "2026-10-14"}, ctx)
    assert ctx.calendar_places == {"1 Example Street, Springfield", "48.8584,2.2945"}


async def test_bad_date_is_reported_to_the_model(ctx):
    (tool,) = build_calendar_tools(FakeCalendar([]), "Europe/Paris")
    with pytest.raises(ToolError):
        await tool.handler({"date": "tomorrow"}, ctx)


def test_the_default_calendar_is_read_from_the_servers_answer():
    from lxml import etree

    answer = etree.fromstring(
        b'<C:schedule-default-calendar-URL xmlns:C="urn:ietf:params:xml:ns:caldav" '
        b'xmlns:D="DAV:"><D:href>/1234/calendars/home/</D:href>'
        b"</C:schedule-default-calendar-URL>"
    )
    assert default_calendar_path(answer) == "/1234/calendars/home"
    assert default_calendar_path(None) == ""
    # Some servers (seen on iCloud) give the path as the element's own text.
    bare = etree.fromstring(
        b'<schedule-default-calendar-URL xmlns="urn:ietf:params:xml:ns:caldav">'
        b"/1234/calendars/home/</schedule-default-calendar-URL>"
    )
    assert default_calendar_path(bare) == "/1234/calendars/home"


class WritableCalendar:
    def __init__(self) -> None:
        self.added: list[bytes] = []

    async def add_event(self, ical: bytes) -> str:
        self.added.append(ical)
        return "Home"


async def test_an_event_goes_to_the_default_calendar_in_local_time(ctx):
    calendar = WritableCalendar()
    (tool,) = build_calendar_write_tools(calendar, "Europe/Paris")  # type: ignore[arg-type]
    result = await tool.handler(
        {"title": "Dentist", "start": "2026-10-27 10:00", "location": "North Beach"}, ctx
    )
    await tool.handler({"title": "Holidays", "start": "2026-11-01", "all_day": True}, ctx)
    with pytest.raises(ToolError, match="after the start"):
        await tool.handler(
            {"title": "x", "start": "2026-10-27 10:00", "end": "2026-10-27 09:00"}, ctx
        )
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await tool.handler({"title": "x", "start": "next Tuesday"}, ctx)

    assert result == {"added_to": "Home", "title": "Dentist", "start": "2026-10-27T10:00+01:00"}
    timed = icalendar.Calendar.from_ical(calendar.added[0]).walk("VEVENT")[0]
    assert timed.decoded("dtstart") == datetime(2026, 10, 27, 10, 0, tzinfo=PARIS)
    assert timed.decoded("dtend") - timed.decoded("dtstart") == timedelta(hours=1)
    assert str(timed["location"]) == "North Beach"
    assert str(timed["uid"]).endswith("@intelligent-home-agent")
    day = icalendar.Calendar.from_ical(calendar.added[1]).walk("VEVENT")[0]
    assert day.decoded("dtstart") == date(2026, 11, 1) and day.decoded("dtend") == date(2026, 11, 2)
    assert tool.proposable and tool.needs_clean_turn


async def test_an_event_from_an_email_shows_every_detail_before_it_is_added():
    args = {
        "title": "Visit",
        "start": "2026-10-27 18:00",
        "end": "2026-10-27 19:30",
        "location": "North Beach",
        "notes": "Code 12",
    }
    (tool,) = build_calendar_write_tools(WritableCalendar(), "Europe/Paris")  # type: ignore[arg-type]
    (french,) = build_calendar_write_tools(WritableCalendar(), "Europe/Paris", "fr")  # type: ignore[arg-type]

    assert await tool.summary(args) == (
        'Add to the calendar "Visit", from 2026-10-27T18:00+01:00 to '
        "2026-10-27T19:30+01:00, at North Beach (notes: Code 12)"
    )
    assert await french.summary(args) == (
        "Ajouter au calendrier « Visit », du 2026-10-27T18:00+01:00 au "
        "2026-10-27T19:30+01:00, à North Beach (notes : Code 12)"
    )


async def test_an_event_after_reading_mail_waits_for_the_users_go_then_is_added(ctx):
    calendar = WritableCalendar()
    registry = ToolRegistry(build_calendar_write_tools(calendar, "Europe/Paris"))  # type: ignore[arg-type]
    registry.extend(build_proposal_tools(registry))
    conv = ctx.conversation
    ctx.tainted = True  # an invitation was read
    args = {"title": "Visit", "start": "2026-10-27 18:00"}

    parked = await registry.run("create_calendar_event", args, ctx)

    assert parked["status"] == "waiting_for_the_user" and calendar.added == []
    assert 'Add to the calendar "Visit"' in with_waiting("Here.", ctx.actions, conv)
    conv.turn += 1
    result = await registry.run("run_proposals", {}, ToolContext(conversation=conv))
    assert result["failed"] == [] and len(calendar.added) == 1


class FakeCal:
    def __init__(self, name, path, components=("VEVENT",)):
        self.name, self.url, self.components = name, f"https://dav.example.com{path}/", components
        self.added: list[str] = []

    def get_display_name(self):
        return self.name

    def get_supported_components(self):
        return list(self.components)

    def add_event(self, ical):
        self.added.append(ical)


def fake_server(monkeypatch, calendars, default=None):
    """A CalDAV account with these calendars; ``default``: the path its inbox points at."""

    class Inbox:
        def get_property(self, _prop, parse_props=False):
            if default is None:
                raise LookupError("no such property")
            return default

    class Principal:
        def calendars(self):
            return calendars

        def schedule_inbox(self):
            return Inbox()

    class Client:
        def __init__(self, **_kwargs):
            pass

        def principal(self):
            return Principal()

    monkeypatch.setattr("intelligent_home_agent.tools.calendar.caldav.DAVClient", Client)


async def test_new_events_go_to_the_servers_default_calendar(monkeypatch):
    tasks = FakeCal("Tasks", "/1/calendars/tasks", ("VTODO",))
    work, home = FakeCal("Work", "/1/calendars/work"), FakeCal("Home", "/1/calendars/home")
    fake_server(monkeypatch, [tasks, work, home], default="/1/calendars/home/")
    calendar = CalDavCalendar("https://dav.example.com", "sam", "pw", [])

    assert await calendar.add_event(b"BEGIN:VCALENDAR") == "Home"
    assert home.added == ["BEGIN:VCALENDAR"] and work.added == []


async def test_without_a_server_default_the_first_calendar_that_takes_events_is_used(monkeypatch):
    tasks = FakeCal("Tasks", "/1/calendars/tasks", ("VTODO",))
    work, home = FakeCal("Work", "/1/calendars/work"), FakeCal("Home", "/1/calendars/home")
    fake_server(monkeypatch, [tasks, work, home])

    assert await CalDavCalendar("https://dav.example.com", "sam", "pw", []).default_name() == "Work"
    # Among the calendars the settings keep, when they name some.
    kept = CalDavCalendar("https://dav.example.com", "sam", "pw", ["home"])
    assert await kept.default_name() == "Home"


async def test_the_calendar_named_in_the_settings_wins_over_the_servers_default(monkeypatch):
    work, home = FakeCal("Work", "/1/calendars/work"), FakeCal("Home", "/1/calendars/home")
    fake_server(monkeypatch, [work, home], default="/1/calendars/home/")

    named = CalDavCalendar("https://dav.example.com", "sam", "pw", [], "work")
    missing = CalDavCalendar("https://dav.example.com", "sam", "pw", [], "Family trips")

    assert await named.add_event(b"BEGIN:VCALENDAR") == "Work"
    with pytest.raises(ToolError, match="No calendar named Family trips.*Work.*Home"):
        await missing.add_event(b"BEGIN:VCALENDAR")
    assert len(work.added) == 1 and home.added == []
