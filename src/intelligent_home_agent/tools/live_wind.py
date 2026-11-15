"""Wind measured right now near the user's spots, from anemometers (not forecasts).

Each source is optional and named in LIVE_WIND_SOURCES; none is on by default.

- ``openwindmap``: OpenWindMap (the Pioupiou network), community wind beacons as open data,
  no key. Any country, but the beacons are where volunteers put them: dense in France and
  around the Alps, sparse elsewhere.
- ``metar``: the airports' routine weather reports, from aviationweather.gov, no key.
  Worldwide, one reading every 30 to 60 minutes, and an airport may be far from the water.
- ``meteofrance``: Météo-France's official stations, a reading every 6 minutes. France
  only, and it needs a free application id (METEOFRANCE_APPLICATION_ID).

A new source is a class with ``name``, ``max_age`` and ``nearest(lat, lon, radius_km,
now)``, added to ``build_live_sources``. A site that shows its readings to its own
subscribers only is not one of them (see docs/outdoor.md).

Only numbers, compass points and station identifiers come out of these sources (plus the
official station names of Météo-France, reduced to letters): nothing written by others
reaches the model.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.config import HomeNow, Place, home_now, in_season
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.weather import (
    compass_degrees,
    compass_point,
    direction_matches,
    named_spots,
)

OPENWINDMAP_URL = "https://api.pioupiou.fr/v1/live/all"
METAR_URL = "https://aviationweather.gov/api/data/metar"
METEOFRANCE_TOKEN_URL = "https://portail-api.meteofrance.fr/token"  # noqa: S105 - a URL
METEOFRANCE_API_URL = "https://public-api.meteofrance.fr/public/DPPaquetObs/v2"

KMH_TO_KNOTS = 1 / 1.852
MS_TO_KNOTS = 3600 / 1852
# Older than this, a reading no longer tells whether it blows now.
MAX_AGE = timedelta(minutes=45)
# Airports report every 30 to 60 minutes.
METAR_MAX_AGE = timedelta(minutes=90)
# A 6-minute package fills up over the next ~10 minutes: the latest readings come from
# merging the most recent ones.
_MF_STEP = timedelta(minutes=6)
_MF_PACKAGES = 3
_MF_STATIONS_TTL = 24 * 3600
_SHARED_TTL = 60
_METAR_TTL = 300
_STATION_NAME_CHARS = 40
_ICAO = re.compile(r"^[A-Z0-9]{4}$")
_NOT_A_NAME = re.compile(r"[^\w '-]+")

Clock = Callable[[], datetime]


@dataclass
class Reading:
    source: str
    station: str
    observed_at: datetime
    avg_knots: float
    gust_knots: float | None
    direction: str | None
    distance_km: float | None = None

    def report(self, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "station": self.station,
            "observed_at": self.observed_at.astimezone(tz).strftime("%H:%M"),
            "minutes_ago": max(0, round((now - self.observed_at).total_seconds() / 60)),
            "avg_knots": round(self.avg_knots),
            "gust_knots": None if self.gust_knots is None else round(self.gust_knots),
            "direction": self.direction,
        }
        if self.distance_km is not None:
            out["distance_km"] = round(self.distance_km, 1)
        return out


class LiveSource(Protocol):
    name: str
    max_age: timedelta

    async def nearest(
        self, lat: float, lon: float, radius_km: float, now: datetime
    ) -> Reading | None: ...


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _direction(degrees: Any) -> str | None:
    value = _number(degrees)
    return None if value is None else compass_point(value % 360)


def plain_name(text: str, limit: int = _STATION_NAME_CHARS) -> str:
    """A station's name reduced to letters, digits, spaces, hyphens and apostrophes."""
    return " ".join(_NOT_A_NAME.sub(" ", text).split())[:limit].strip()


# --- OpenWindMap -----------------------------------------------------------------------


class OpenWindMap:
    name = "OpenWindMap"
    max_age = MAX_AGE

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._cache: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def _all(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < _SHARED_TTL:
                return self._cache[1]
            try:
                resp = await self._http.get(OPENWINDMAP_URL)
                resp.raise_for_status()
                data = resp.json().get("data") or []
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                raise ToolError(f"OpenWindMap unavailable: {exc}") from exc
            self._cache = (time.monotonic(), data if isinstance(data, list) else [])
            return self._cache[1]

    async def nearest(
        self, lat: float, lon: float, radius_km: float, now: datetime
    ) -> Reading | None:
        candidates: list[Reading] = []
        for beacon in await self._all():
            try:
                if (beacon.get("status") or {}).get("state") != "on":
                    continue
                where, wind = beacon["location"], beacon["measurements"]
                b_lat, b_lon = _number(where["latitude"]), _number(where["longitude"])
                avg = _number(wind.get("wind_speed_avg"))
                observed = datetime.fromisoformat(str(wind["date"]).replace("Z", "+00:00"))
                beacon_id = int(beacon["id"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if b_lat is None or b_lon is None or avg is None or now - observed > self.max_age:
                continue
            km = distance_km(lat, lon, b_lat, b_lon)
            if km > radius_km:
                continue
            gust = _number(wind.get("wind_speed_max"))
            candidates.append(
                Reading(
                    # Beacon names are written by their owners: only the number goes out.
                    source=self.name,
                    station=f"Pioupiou #{beacon_id}",
                    observed_at=observed,
                    avg_knots=avg * KMH_TO_KNOTS,
                    gust_knots=None if gust is None else gust * KMH_TO_KNOTS,
                    direction=_direction(wind.get("wind_heading")),
                    distance_km=km,
                )
            )
        return min(candidates, key=lambda r: r.distance_km or 0, default=None)


# --- Airport reports (METAR) -----------------------------------------------------------


class Metar:
    name = "METAR"
    max_age = METAR_MAX_AGE

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._cache: dict[tuple[float, float, float], tuple[float, list[Any]]] = {}

    async def _around(self, lat: float, lon: float, radius_km: float) -> list[Any]:
        key = (round(lat, 3), round(lon, 3), radius_km)
        if (kept := self._cache.get(key)) and time.monotonic() - kept[0] < _METAR_TTL:
            return kept[1]
        d_lat = radius_km / 111.0
        d_lon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.05))
        box = (max(lat - d_lat, -90), max(lon - d_lon, -180), min(lat + d_lat, 90), lon + d_lon)
        try:
            resp = await self._http.get(
                METAR_URL,
                params={
                    "bbox": ",".join(f"{v:.3f}" for v in (*box[:3], min(box[3], 180))),
                    "format": "json",
                    "hours": 2,
                },
            )
            resp.raise_for_status()
            # No airport in the box: 204 and no body.
            rows = resp.json() if resp.status_code == 200 and resp.content else []
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError(f"Airport reports unavailable: {exc}") from exc
        rows = rows if isinstance(rows, list) else []
        self._cache[key] = (time.monotonic(), rows)
        return rows

    async def nearest(
        self, lat: float, lon: float, radius_km: float, now: datetime
    ) -> Reading | None:
        latest: dict[str, Reading] = {}
        for row in await self._around(lat, lon, radius_km):
            if not isinstance(row, dict):
                continue
            station = str(row.get("icaoId") or "")
            s_lat, s_lon = _number(row.get("lat")), _number(row.get("lon"))
            avg, when = _number(row.get("wspd")), _number(row.get("obsTime"))
            if not _ICAO.match(station) or None in (s_lat, s_lon, avg, when):
                continue
            observed = datetime.fromtimestamp(when, UTC)
            km = distance_km(lat, lon, s_lat, s_lon)
            if km > radius_km or now - observed > self.max_age:
                continue
            if station in latest and latest[station].observed_at >= observed:
                continue
            latest[station] = Reading(
                source=self.name,
                station=station,
                observed_at=observed,
                avg_knots=avg,
                gust_knots=_number(row.get("wgst")),
                # "VRB" (variable) is no direction.
                direction=_direction(row.get("wdir")),
                distance_km=km,
            )
        return min(latest.values(), key=lambda r: r.distance_km or 0, default=None)


# --- Météo-France (France only) --------------------------------------------------------


@dataclass
class _Station:
    id: str
    name: str
    lat: float
    lon: float


class MeteoFrance:
    name = "Météo-France"
    max_age = MAX_AGE

    def __init__(self, http: httpx.AsyncClient, application_id: str) -> None:
        self._http = http
        self._application_id = application_id
        self._token: tuple[str, float] | None = None
        self._stations: tuple[float, list[_Station]] | None = None
        self._latest: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def _access_token(self, refresh: bool = False) -> str:
        if not refresh and self._token and self._token[1] > time.monotonic() + 60:
            return self._token[0]
        try:
            resp = await self._http.post(
                METEOFRANCE_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {self._application_id}"},
            )
            resp.raise_for_status()
            body = resp.json()
            token = str(body["access_token"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ToolError(f"Météo-France login failed: {exc}") from exc
        self._token = (token, time.monotonic() + float(body.get("expires_in") or 3600))
        return token

    async def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        for refresh in (False, True):
            token = await self._access_token(refresh)
            try:
                resp = await self._http.get(
                    f"{METEOFRANCE_API_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"Météo-France unavailable: {exc}") from exc
            if resp.status_code != 401:
                break
        if resp.status_code != 200:
            raise ToolError(f"Météo-France unavailable: HTTP {resp.status_code}")
        return resp

    async def _station_list(self) -> list[_Station]:
        if self._stations and time.monotonic() - self._stations[0] < _MF_STATIONS_TTL:
            return self._stations[1]
        resp = await self._get("/liste-stations")
        text = resp.content.decode("latin-1")
        stations = []
        for row in csv.DictReader(io.StringIO(text), delimiter=";"):
            lat, lon = _number(row.get("Latitude")), _number(row.get("Longitude"))
            station_id = (row.get("Id_station") or "").strip()
            if lat is None or lon is None or not station_id.isdigit():
                continue
            name = plain_name((row.get("Nom_usuel") or "").title())
            stations.append(_Station(station_id, name or station_id, lat, lon))
        self._stations = (time.monotonic(), stations)
        return stations

    async def _latest_rows(self, now: datetime) -> dict[str, dict[str, Any]]:
        if self._latest and time.monotonic() - self._latest[0] < _SHARED_TTL:
            return self._latest[1]
        step = now.replace(second=0, microsecond=0)
        step -= timedelta(minutes=step.minute % 6)

        async def package(back: int) -> list[Any]:
            date = (step - back * _MF_STEP).strftime("%Y-%m-%dT%H:%M:00Z")
            resp = await self._get(
                "/paquet/stations/infrahoraire-6m", {"date": date, "format": "json"}
            )
            rows = resp.json()
            return rows if isinstance(rows, list) else []

        results = await asyncio.gather(
            *(package(b) for b in range(_MF_PACKAGES)), return_exceptions=True
        )
        packages = [r for r in results if isinstance(r, list)]
        if not packages:
            raise next(r for r in results if isinstance(r, BaseException))
        latest: dict[str, dict[str, Any]] = {}
        for rows in packages:
            for row in rows:
                if not isinstance(row, dict) or _number(row.get("ff")) is None:
                    continue
                station_id = str(row.get("geo_id_insee", "")).strip()
                kept = latest.get(station_id)
                if kept is None or str(row.get("validity_time")) > str(kept.get("validity_time")):
                    latest[station_id] = row
        self._latest = (time.monotonic(), latest)
        return latest

    async def nearest(
        self, lat: float, lon: float, radius_km: float, now: datetime
    ) -> Reading | None:
        async with self._lock:
            distances = [
                (distance_km(lat, lon, s.lat, s.lon), s) for s in await self._station_list()
            ]
            near = sorted((d for d in distances if d[0] <= radius_km), key=lambda d: d[0])
            if not near:
                return None
            latest = await self._latest_rows(now)
        for km, station in near:
            row = latest.get(station.id)
            if row is None:
                continue
            try:
                observed = datetime.fromisoformat(str(row["validity_time"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if now - observed > self.max_age:
                continue
            gust = _number(row.get("raf10"))
            return Reading(
                source=self.name,
                station=station.name,
                observed_at=observed,
                avg_knots=float(row["ff"]) * MS_TO_KNOTS,
                gust_knots=None if gust is None else gust * MS_TO_KNOTS,
                direction=_direction(row.get("dd")),
                distance_km=km,
            )
        return None


# --- Tool ------------------------------------------------------------------------------


def build_live_sources(
    http: httpx.AsyncClient, names: list[str], *, meteofrance_application_id: str | None = None
) -> list[LiveSource]:
    """The sources named in LIVE_WIND_SOURCES; one that lacks its key is left out."""
    sources: list[LiveSource] = []
    for name in names:
        if name == "openwindmap":
            sources.append(OpenWindMap(http))
        elif name == "metar":
            sources.append(Metar(http))
        elif name == "meteofrance" and meteofrance_application_id:
            sources.append(MeteoFrance(http, meteofrance_application_id))
    return sources


Reporter = Callable[[Any, datetime], Awaitable[dict[str, Any]]]


def live_wind_reporter(sources: list[LiveSource], *, timezone: str, radius_km: float) -> Reporter:
    """What the anemometers near a spot measure now: get_live_wind's reports, and the
    activity alerts' (activity_alerts.py). A spot is anything with a name, a latitude and
    a longitude; a wind spot also has its good directions."""
    tz = ZoneInfo(timezone)

    async def spot_report(spot: Any, now: datetime) -> dict[str, Any]:
        results = await asyncio.gather(
            *(s.nearest(spot.latitude, spot.longitude, radius_km, now) for s in sources),
            return_exceptions=True,
        )
        readings, problems = [], []
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, ToolError):
                problems.append(f"{source.name}: {result}")
            elif isinstance(result, BaseException):
                problems.append(f"{source.name}: unavailable")
            elif result is None:
                problems.append(f"{source.name}: no recent reading within {radius_km:g} km")
            else:
                readings.append(result.report(now, tz))
        report: dict[str, Any] = {"name": spot.name, "readings": readings}
        directions = getattr(spot, "directions", None)
        if hasattr(spot, "min_knots"):
            report["min_knots"] = spot.min_knots
            report["good_directions"] = directions or "any"
        for reading in readings:
            if directions and reading["direction"]:
                degrees = compass_degrees(reading["direction"])
                reading["direction_ok"] = direction_matches(degrees, directions)
        if problems:
            report["unavailable"] = list(dict.fromkeys(problems))
        return report

    return spot_report


@dataclass(frozen=True)
class _Home:
    latitude: float
    longitude: float
    name: str = "home"


def build_live_wind_tools(
    spots: list[Any],
    sources: list[LiveSource],
    *,
    timezone: str,
    radius_km: float,
    home: Place | HomeNow | None = None,
    clock: Clock = lambda: datetime.now(UTC),
) -> list[Tool]:
    """``spots``: the wind and surf spots. Without any, the wind near home."""
    where = home_now(home)
    if not sources or not (spots or where() is not None):
        return []
    tz = ZoneInfo(timezone)
    spot_report = live_wind_reporter(sources, timezone=timezone, radius_km=radius_km)

    async def get_live_wind(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        now = clock()
        here = where()
        if name := args.get("spot"):
            selected = named_spots(spots, name)
            if not selected and name.casefold().strip() == "home" and here is not None:
                selected = [_Home(*here)]
            if not selected:
                raise ToolError(f"Unknown spot {name}. Known spots: {[s.name for s in spots]}")
        elif spots:
            selected = [s for s in spots if in_season(s, now.astimezone(tz).date())] or spots
        else:
            selected = [_Home(*here)] if here is not None else []
        reports = await asyncio.gather(*(spot_report(s, now) for s in selected))
        return {"unit": "knots", "now": now.astimezone(tz).strftime("%H:%M"), "spots": reports}

    known = ", ".join(s.name for s in spots) or "none, the wind near home is given"
    return [
        Tool(
            name="get_live_wind",
            description=(
                "Wind measured right now by anemometers near the user's spots "
                f"({', '.join(s.name for s in sources)}): mean wind and gusts in knots, "
                "direction, time of the reading and distance from the spot. Use it for 'is "
                "it windy right now?' or to check a forecast before going out today. A "
                "reading may come from kilometres away: say the distance and the time. "
                'Without a spot, those in season now; "home" for the wind near home. '
                f"Known spots: {known}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "spot": {"type": "string", "description": "One spot name; omit for all."},
                },
            },
            handler=get_live_wind,
        )
    ]
