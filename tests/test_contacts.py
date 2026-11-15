from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError, build_calendar_tools
from intelligent_home_agent.tools.calendar import CalendarEvent
from intelligent_home_agent.tools.contacts import (
    Birthday,
    CardDavBirthdays,
    MergedCalendar,
    birthday_events,
    parse_vcard,
)

PARIS = ZoneInfo("Europe/Paris")
CARDDAV = "https://contacts.example.com"
# Like iCloud, the server sends the client to another host for the address books.
HOME = "https://p42-contacts.example.com/123/carddavhome/"


def vcard(*lines):
    return "\r\n".join(["BEGIN:VCARD", "VERSION:3.0", *lines, "END:VCARD"])


def test_parse_vcard_with_year():
    card = vcard("N:Martin;Zoé;;;", "FN:Zoé Martin", "BDAY;VALUE=date:1990-10-18")
    assert parse_vcard(card) == Birthday("Zoé Martin", 10, 18, 1990)


def test_parse_vcard_apple_omitted_year_and_folded_name():
    card = vcard("FN:Mary-Jane Wat", " son", "BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-05")
    assert parse_vcard(card) == Birthday("Mary-Jane Watson", 3, 5, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("19901018", (10, 18, 1990)), ("--1018", (10, 18, None)), ("--10-18", (10, 18, None))],
)
def test_parse_vcard_date_formats(value, expected):
    assert parse_vcard(vcard("FN:Zoé", f"BDAY:{value}")) == Birthday("Zoé", *expected)


def test_parse_vcard_falls_back_to_n_and_skips_contacts_without_birthday():
    assert parse_vcard(vcard("N:Martin;Zoé;;;", "FN:", "BDAY:2000-01-02")).name == "Zoé Martin"
    assert parse_vcard(vcard("FN:Zoé")) is None
    assert parse_vcard(vcard("FN:Zoé", "BDAY:not a date")) is None


def test_birthday_events_in_range_with_age():
    birthdays = [Birthday("Zoé", 10, 18, 1990), Birthday("Tom", 10, 20), Birthday("Max", 1, 1)]
    start = datetime(2026, 10, 18, tzinfo=PARIS)

    events = birthday_events(birthdays, start, start + timedelta(days=3), PARIS)

    assert [(e.title, e.start) for e in events] == [
        ("Zoé's birthday (36)", date(2026, 10, 18)),
        ("Tom's birthday", date(2026, 10, 20)),
    ]
    assert all(e.all_day and e.calendar == "Birthdays" for e in events)


def test_leap_day_birthday_on_28_february_in_common_years():
    start = datetime(2027, 2, 28, tzinfo=PARIS)
    events = birthday_events([Birthday("Zoé", 2, 29)], start, start + timedelta(days=2), PARIS)
    assert [e.start for e in events] == [date(2027, 2, 28)]


def multistatus(*responses):
    return (
        '<?xml version="1.0"?><multistatus xmlns="DAV:" '
        'xmlns:card="urn:ietf:params:xml:ns:carddav">' + "".join(responses) + "</multistatus>"
    )


def card_names(cards):
    return [f"{i}.vcf" if i % 2 else f"card-{i}" for i in range(len(cards))]


def mock_server(mock, cards):
    mock.route(method="PROPFIND", url=f"{CARDDAV}/").respond(
        207,
        text=multistatus(
            "<response><href>/</href><propstat><prop><current-user-principal>"
            "<href>/123/principal/</href></current-user-principal></prop></propstat></response>"
        ),
    )
    mock.route(method="PROPFIND", url=f"{CARDDAV}/123/principal/").respond(
        207,
        text=multistatus(
            "<response><href>/123/principal/</href><propstat><prop><card:addressbook-home-set>"
            f"<href>{HOME}</href></card:addressbook-home-set></prop></propstat></response>"
        ),
    )
    mock.route(method="PROPFIND", url=HOME).respond(
        207,
        text=multistatus(
            f"<response><href>{HOME}</href><propstat><prop><resourcetype><collection/>"
            "</resourcetype></prop></propstat></response>"
            "<response><href>/123/carddavhome/card/</href><propstat><prop><resourcetype>"
            "<collection/><card:addressbook/></resourcetype></prop></propstat></response>"
        ),
    )
    book = "https://p42-contacts.example.com/123/carddavhome/card/"
    mock.route(method="PROPFIND", url=book).respond(
        207,
        text=multistatus(
            "<response><href>/123/carddavhome/card/</href><propstat><prop><resourcetype>"
            "<collection/><card:addressbook/></resourcetype></prop></propstat></response>"
            "<response><href>/123/carddavhome/card/groups/</href><propstat><prop>"
            "<resourcetype><collection/></resourcetype></prop></propstat></response>"
            # Card names differ by server: with or without .vcf.
            + "".join(
                f"<response><href>/123/carddavhome/card/{name}</href><propstat><prop>"
                "<resourcetype/></prop></propstat></response>"
                for name in card_names(cards)
            )
        ),
    )
    return mock.route(method="REPORT", url=book).respond(
        207,
        text=multistatus(
            *(
                f"<response><href>/123/carddavhome/card/{name}</href><propstat><prop>"
                f"<card:address-data>{card}</card:address-data></prop></propstat></response>"
                for name, card in zip(card_names(cards), cards, strict=True)
            )
        ),
    )


async def test_birthdays_are_read_from_the_contacts_once():
    cards = [vcard("FN:Zoé Martin", "BDAY:1990-10-18"), vcard("FN:No Date")]
    async with httpx.AsyncClient() as http:
        with respx.mock() as mock:
            report = mock_server(mock, cards)
            source = CardDavBirthdays(http, CARDDAV, "sam@example.com", "pw", "Europe/Paris")
            start = datetime(2026, 10, 18, tzinfo=PARIS)

            events = await source.events(start, start + timedelta(days=1))
            await source.events(start, start + timedelta(days=1))

    assert [e.title for e in events] == ["Zoé Martin's birthday (36)"]
    assert report.call_count == 1
    asked = report.calls[0].request.content.decode()
    assert "/card/card-0</d:href>" in asked and "/card/1.vcf</d:href>" in asked
    assert "/card/groups/" not in asked and "/card/</d:href>" not in asked
    assert report.calls[0].request.headers["authorization"].startswith("Basic ")


async def test_a_refused_login_is_a_tool_error():
    async with httpx.AsyncClient() as http:
        with respx.mock() as mock:
            mock.route(method="PROPFIND").respond(401)
            source = CardDavBirthdays(http, CARDDAV, "sam@example.com", "bad", "Europe/Paris")
            with pytest.raises(ToolError, match="401"):
                await source.events(datetime(2026, 10, 18, tzinfo=PARIS), datetime.now(PARIS))


class Source:
    def __init__(self, events=None, error=None):
        self._events, self._error = events or [], error

    async def events(self, start, end):
        if self._error:
            raise self._error
        return self._events


async def test_birthday_shows_in_calendar_tool_when_one_source_fails(ctx):
    birthday = CalendarEvent("Zoé's birthday", date(2026, 10, 18), None, "Birthdays")
    merged = MergedCalendar([Source(error=ToolError("Calendar unavailable")), Source([birthday])])
    (tool,) = build_calendar_tools(merged, "Europe/Paris")

    result = await tool.handler({"date": "2026-10-18"}, ctx)

    assert [e["title"] for e in result["events"]] == ["Zoé's birthday"]
    assert result["events"][0]["all_day"]


async def test_merged_calendar_fails_when_every_source_fails():
    merged = MergedCalendar([Source(error=ToolError("a")), Source(error=ToolError("b"))])
    with pytest.raises(ToolError):
        await merged.events(datetime(2026, 10, 18, tzinfo=PARIS), datetime.now(PARIS))
