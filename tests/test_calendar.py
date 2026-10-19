from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest

from intelligent_home_agent.tools import ToolError, build_calendar_tools
from intelligent_home_agent.tools.calendar import CalendarEvent, event_from_component

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
            CalendarEvent("Alex's birthday", date(2026, 10, 14), date(2026, 10, 15), "Personal"),
            CalendarEvent(
                "Client call", datetime(2026, 10, 14, 7, 0, tzinfo=ZoneInfo("UTC")), None, "Work"
            ),
        ]
    )
    (tool,) = build_calendar_tools(source, "Europe/Paris")

    result = await tool.handler({"date": "2026-10-14", "days": 1}, ctx)

    assert [e["title"] for e in result["events"]] == ["Alex's birthday", "Client call", "Meeting"]
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
