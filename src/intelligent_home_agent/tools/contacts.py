"""Birthdays from the user's contacts over CardDAV (iCloud, Fastmail, Nextcloud...).

The Birthdays calendar a provider shows is generated from the contacts and usually not
served over CalDAV (iCloud's is not), so the birthdays are read from the contacts and
turned into all-day calendar events.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urljoin
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
from lxml import etree

from intelligent_home_agent.tools.base import ToolError
from intelligent_home_agent.tools.calendar import CalendarEvent, CalendarSource

log = logging.getLogger(__name__)

BIRTHDAYS_CALENDAR = "Birthdays"
# Contacts rarely change: read them at most this often.
CACHE_SECONDS = 6 * 3600
_MULTIGET_BATCH = 200
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)
# 1990-10-18, 19901018, --10-18, --1018, optionally followed by a time.
_BDAY_RE = re.compile(r"^(?:(\d{4})|--)-?(\d{2})-?(\d{2})")
# Apple writes BDAY;X-APPLE-OMIT-YEAR=1604:1604-10-18 when the year is unknown.
_OMIT_YEAR_RE = re.compile(r"X-APPLE-OMIT-YEAR=(\d{4})", re.IGNORECASE)


@dataclass(frozen=True)
class Birthday:
    name: str
    month: int
    day: int
    year: int | None = None


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def parse_vcard(text: str) -> Birthday | None:
    """The contact's birthday, or None when it has none."""
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    name = bday = params = None
    family_given = None
    for line in lines:
        head, sep, value = line.partition(":")
        if not sep:
            continue
        # Drop an item group prefix such as "item1.".
        prop, _, line_params = head.rpartition(".")[2].partition(";")
        prop = prop.upper()
        if prop == "FN" and value.strip():
            name = _unescape(value.strip())
        elif prop == "N":
            parts = [_unescape(p).strip() for p in re.split(r"(?<!\\);", value)]
            family_given = " ".join(p for p in (parts[1:2] + parts[:1]) if p)
        elif prop == "BDAY":
            bday, params = value.strip(), line_params
    if not bday:
        return None
    match = _BDAY_RE.match(bday)
    if not match:
        return None
    year = int(match.group(1)) if match.group(1) else None
    omitted = _OMIT_YEAR_RE.search(params or "")
    if omitted and year == int(omitted.group(1)):
        year = None
    month, day = int(match.group(2)), int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return Birthday(name or family_given or "(unnamed)", month, day, year)


def _on(birthday: Birthday, year: int) -> date:
    try:
        return date(year, birthday.month, birthday.day)
    except ValueError:  # 29 February in a common year
        return date(year, 2, 28)


def birthday_events(
    birthdays: list[Birthday], start: datetime, end: datetime, tz: ZoneInfo
) -> list[CalendarEvent]:
    """One all-day event per birthday that falls on a day in [start, end)."""
    first = start.astimezone(tz).date()
    events = []
    day = first
    while datetime.combine(day, time.min, tz) < end:
        for b in birthdays:
            if _on(b, day.year) != day:
                continue
            title = f"{b.name}'s birthday"
            if b.year and day.year > b.year:
                title += f" ({day.year - b.year})"
            events.append(CalendarEvent(title, day, day + timedelta(days=1), BIRTHDAYS_CALENDAR))
        day += timedelta(days=1)
    return events


def _propfind_body(*props: str) -> str:
    inner = "".join(f"<{p}/>" for p in props)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        f"<d:prop>{inner}</d:prop></d:propfind>"
    )


class CardDavBirthdays:
    """Reads every contact's birthday over CardDAV, cached for a few hours. ``url`` is the
    server's CardDAV address: the address books are found from it (RFC 6352)."""

    def __init__(
        self, http: httpx.AsyncClient, url: str, username: str, password: str, timezone: str
    ) -> None:
        self._http = http
        self._url = url.rstrip("/") + "/"
        self._auth = httpx.BasicAuth(username, password)
        self._tz = ZoneInfo(timezone)
        self._birthdays: list[Birthday] | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def _request(self, method: str, url: str, body: str, depth: str) -> etree._Element:
        response = await self._http.request(
            method,
            url,
            content=body.encode(),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            auth=self._auth,
            timeout=20,
        )
        if response.status_code != 207:
            raise ToolError(f"Contacts unavailable: HTTP {response.status_code}")
        return etree.fromstring(response.content, _PARSER)  # noqa: S320

    async def _href(self, url: str, prop: str) -> str:
        root = await self._request("PROPFIND", url, _propfind_body(prop), "0")
        href = root.find(f".//{prop}/d:href", _NS)
        if href is None or not href.text:
            raise ToolError("Contacts unavailable: no address book found")
        return urljoin(url, href.text.strip())

    async def _address_books(self) -> list[str]:
        principal = await self._href(self._url, "d:current-user-principal")
        home = await self._href(principal, "c:addressbook-home-set")
        root = await self._request("PROPFIND", home, _propfind_body("d:resourcetype"), "1")
        return [
            urljoin(home, r.findtext("d:href", "", _NS).strip())
            for r in root.findall("d:response", _NS)
            if r.find(".//d:resourcetype/c:addressbook", _NS) is not None
        ]

    async def _cards(self, book: str) -> list[str]:
        root = await self._request(
            "PROPFIND", book, _propfind_body("d:getetag", "d:resourcetype"), "1"
        )
        # Every card of the book, whatever its name ends with: not the book itself nor a
        # collection inside it.
        hrefs = [
            href
            for r in root.findall("d:response", _NS)
            if (href := r.findtext("d:href", "", _NS).strip())
            and urljoin(book, href).rstrip("/") != book.rstrip("/")
            and r.find(".//d:resourcetype/d:collection", _NS) is None
        ]
        cards = []
        for i in range(0, len(hrefs), _MULTIGET_BATCH):
            refs = "".join(f"<d:href>{escape(h)}</d:href>" for h in hrefs[i : i + _MULTIGET_BATCH])
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<c:addressbook-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
                f"<d:prop><c:address-data/></d:prop>{refs}</c:addressbook-multiget>"
            )
            root = await self._request("REPORT", book, body, "1")
            cards.extend(e.text for e in root.iterfind(".//c:address-data", _NS) if e.text)
        return cards

    async def _load(self) -> list[Birthday]:
        birthdays = []
        for book in await self._address_books():
            for card in await self._cards(book):
                if (b := parse_vcard(card)) is not None:
                    birthdays.append(b)
        return birthdays

    async def birthdays(self) -> list[Birthday]:
        async with self._lock:
            fresh = clock.monotonic() - self._loaded_at < CACHE_SECONDS
            if self._birthdays is None or not fresh:
                try:
                    self._birthdays = await self._load()
                except httpx.HTTPError as exc:
                    raise ToolError(f"Contacts unavailable: {type(exc).__name__}") from exc
                self._loaded_at = clock.monotonic()
            return self._birthdays

    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return birthday_events(await self.birthdays(), start, end, self._tz)


class MergedCalendar:
    """Events from several sources; one failing source doesn't hide the others."""

    def __init__(self, sources: list[CalendarSource]) -> None:
        self._sources = sources

    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        results = await asyncio.gather(
            *(s.events(start, end) for s in self._sources), return_exceptions=True
        )
        events: list[CalendarEvent] = []
        errors = []
        for result in results:
            if isinstance(result, BaseException):
                if not isinstance(result, ToolError):
                    raise result
                log.warning("Calendar source failed: %s", result)
                errors.append(result)
            else:
                events.extend(result)
        if errors and len(errors) == len(results):
            raise errors[0]
        return events
