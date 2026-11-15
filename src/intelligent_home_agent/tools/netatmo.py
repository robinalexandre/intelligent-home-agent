"""The user's Netatmo home: the security cameras (Welcome indoor camera and kin), who the
cameras recognized at home, their latest events, and the weather station or thermostat
readings when there are some, through Netatmo Connect (scripts/netatmo_login.py connects
it once). Turning a camera's monitoring on or off always waits for the user's go.

What the cameras saw is the user's own data, not public web: after it, the request opens
no new site of its own choosing (ToolContext.account_read).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

AUTHORIZE_URL = "https://api.netatmo.com/oauth2/authorize"
TOKEN_URL = "https://api.netatmo.com/oauth2/token"  # noqa: S105 - an endpoint, not a secret
API_URL = "https://api.netatmo.com/api"
SCOPES = "read_camera write_camera read_station read_thermostat"
LOGIN_AGAIN = "run scripts/netatmo_login.py again"
CAMERA_TYPES = {
    "NACamera": "indoor camera",
    "NPC": "indoor camera",
    "NOC": "outdoor camera",
    "NDB": "doorbell",
}
WEATHER_TYPES = {"NAMain", "NAModule1", "NAModule2", "NAModule3", "NAModule4"}
ENERGY_TYPES = {"NATherm1", "NRV", "NAPlug", "OTH", "OTM", "BNS"}
_UNREACHABLE = 6
_TOKEN_ERRORS = (2, 3)
_RATE_LIMITED = (26, 11)
MAX_EVENTS = 30
EVENT_KINDS = {
    "person": "person seen",
    "person_away": "person left",
    "movement": "movement",
    "human": "human",
    "animal": "animal",
    "vehicle": "vehicle",
    "outdoor": "outdoor detection",
    "on": "monitoring on",
    "off": "monitoring off",
    "connection": "camera connected",
    "disconnection": "camera disconnected",
    "boot": "camera restarted",
    "sd": "SD card",
    "alim": "power supply",
    "tag_big_move": "door or window moved",
    "tag_small_move": "door or window touched",
    "tag_open": "door or window open",
    "siren_sounding": "siren",
    "accessory_mode": "accessory",
}
READINGS = {
    "Temperature": "temperature_c",
    "Humidity": "humidity_percent",
    "CO2": "co2_ppm",
    "Noise": "noise_db",
    "Pressure": "pressure_mbar",
    "temp_trend": "temperature_trend",
    "pressure_trend": "pressure_trend",
    "Rain": "rain_mm",
    "sum_rain_1": "rain_last_hour_mm",
    "sum_rain_24": "rain_today_mm",
    "WindStrength": "wind_kmh",
    "WindAngle": "wind_direction_deg",
    "GustStrength": "gust_kmh",
}
_TAG = re.compile(r"<[^>]+>")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError as exc:
        raise ToolError(f"Netatmo sent no answer (HTTP {resp.status_code}).") from exc
    return data if isinstance(data, dict) else {}


class NetatmoAuth:
    """Access tokens from the refresh token; Netatmo may hand a new one on each refresh, so
    the latest is saved each time and only one refresh runs at a time."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        refresh_token: str | None,
        store: Path,
        clock: Any = time.monotonic,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._configured = refresh_token
        self._refresh_token = refresh_token
        self._store = store
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        if store.exists():
            saved = json.loads(store.read_text())
            if saved.get("client_id") == client_id:
                self._refresh_token = saved.get("refresh_token") or refresh_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    async def access_token(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._access_token and self._clock() < self._expires_at - 60:
                return self._access_token
            if not self._refresh_token:
                raise ToolError("Netatmo is not connected: run scripts/netatmo_login.py.")
            body = await self._refresh(self._refresh_token)
            # A saved token the user has since replaced by logging in again: the one in
            # the settings is then the live one.
            if body is None and self._configured and self._configured != self._refresh_token:
                body = await self._refresh(self._configured)
            if body is None:
                raise ToolError(f"Netatmo refused the login: {LOGIN_AGAIN}.")
            self._access_token = body["access_token"]
            self._expires_at = self._clock() + float(body.get("expires_in", 10800))
            if body.get("refresh_token") and body["refresh_token"] != self._refresh_token:
                self._refresh_token = body["refresh_token"]
                self._save()
            return self._access_token

    async def _refresh(self, refresh_token: str) -> dict[str, Any] | None:
        try:
            resp = await self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Netatmo unavailable: {exc}") from exc
        if resp.status_code >= 500:
            raise ToolError(f"Netatmo unavailable (HTTP {resp.status_code}).")
        body = _json(resp)
        if resp.status_code != 200 or "access_token" not in body:
            return None
        if "refresh_token" not in body:
            body["refresh_token"] = refresh_token
        return body

    def _save(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"client_id": self._client_id, "refresh_token": self._refresh_token})
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self._store)


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    kind: str
    home_id: str
    home: str


class Netatmo:
    def __init__(self, http: httpx.AsyncClient, auth: NetatmoAuth) -> None:
        self._http = http
        self._auth = auth

    async def homes(self) -> list[dict[str, Any]]:
        body = await self._call("GET", "/homesdata")
        return [h for h in body.get("homes") or [] if isinstance(h, dict)]

    async def home_status(self, home_id: str) -> dict[str, Any]:
        return await self._call("GET", "/homestatus", params={"home_id": home_id})

    async def events(self, home_id: str) -> list[dict[str, Any]]:
        body = await self._call("GET", "/getevents", params={"home_id": home_id})
        return list((body.get("home") or {}).get("events") or [])

    async def stations(self) -> list[dict[str, Any]]:
        body = await self._call("GET", "/getstationsdata")
        return list(body.get("devices") or [])

    async def cameras(self) -> list[Camera]:
        return [
            Camera(
                str(m["id"]),
                m.get("name") or CAMERA_TYPES[m["type"]],
                CAMERA_TYPES[m["type"]],
                str(home["id"]),
                home.get("name") or "",
            )
            for home in await self.homes()
            for m in home.get("modules") or []
            if m.get("type") in CAMERA_TYPES and m.get("id")
        ]

    async def camera(self, camera_id: str) -> Camera:
        cameras = await self.cameras()
        if not camera_id and len(cameras) == 1:
            return cameras[0]
        for camera in cameras:
            if camera.id.casefold() == camera_id.casefold():
                return camera
        if not cameras:
            raise ToolError("No Netatmo camera on this account.")
        known = ", ".join(f"{c.name} ({c.id})" for c in cameras)
        raise ToolError(f"Which camera? Give its id: {known}.")

    async def set_monitoring(self, camera: Camera, on: bool) -> None:
        body = {
            "home": {
                "id": camera.home_id,
                "modules": [{"id": camera.id, "monitoring": "on" if on else "off"}],
            }
        }
        answer = await self._call("POST", "/setstate", payload=body)
        errors = answer.get("errors") or []
        if any(e.get("code") == _UNREACHABLE for e in errors):
            raise ToolError(f"The camera {camera.name} is unreachable: is it plugged in?")
        if errors:
            raise ToolError(f"Netatmo did not change the camera (error {errors[0].get('code')}).")

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._auth.access_token()
        resp, data = await self._send(method, path, params, payload, token)
        if _code(data) in _TOKEN_ERRORS or resp.status_code == 401:
            token = await self._auth.access_token(force=True)
            resp, data = await self._send(method, path, params, payload, token)
        if resp.status_code == 200 and data.get("status") == "ok":
            body = data.get("body")
            return body if isinstance(body, dict) else {}
        code = _code(data)
        if code in _TOKEN_ERRORS or resp.status_code == 401:
            raise ToolError(f"Netatmo refused the login: {LOGIN_AGAIN}.")
        if code in _RATE_LIMITED or resp.status_code == 429:
            raise ToolError("Netatmo asks to wait: too many requests, try again later.")
        if resp.status_code == 403 or code == 13:
            raise ToolError(
                f"Netatmo refused this request: the app lacks the scope for it, {LOGIN_AGAIN}."
            )
        error = data.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        raise ToolError(f"Netatmo error (HTTP {resp.status_code}, code {code}): {message}.")

    async def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        token: str,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        try:
            resp = await self._http.request(
                method,
                API_URL + path,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Netatmo unavailable: {exc}") from exc
        return resp, _json(resp)


def _code(data: dict[str, Any]) -> int | None:
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else None


def _stamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(float(value)), tz=UTC)
    except (TypeError, ValueError):
        return None


def _text(message: Any) -> str:
    return " ".join(unescape(_TAG.sub("", str(message or ""))).split())


def build_netatmo_tools(netatmo: Netatmo, timezone: str, *, language: str = "en") -> list[Tool]:
    tz = ZoneInfo(timezone)
    s = strings(language)

    def when(value: Any) -> str | None:
        at = _stamp(value)
        return at.astimezone(tz).isoformat(timespec="minutes") if at else None

    def camera_state(module: dict[str, Any], unreachable: set[str]) -> str:
        if module.get("id") in unreachable or module.get("reachable") is False:
            return "disconnected"
        if module.get("status") == "disconnected":
            return "disconnected"
        return str(module.get("monitoring") or module.get("status") or "unknown")

    async def get_home_security_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        homes = [h for h in await netatmo.homes() if _has(h, CAMERA_TYPES)]
        ctx.account_read = True
        if not homes:
            return {"homes": [], "note": "No Netatmo camera on this account."}
        shown = []
        for home in homes:
            status = await netatmo.home_status(str(home["id"]))
            live = {m.get("id"): m for m in (status.get("home") or {}).get("modules") or []}
            unreachable = {
                e.get("id") for e in status.get("errors") or [] if e.get("code") == _UNREACHABLE
            }
            cameras = []
            for module in home.get("modules") or []:
                if module.get("type") not in CAMERA_TYPES:
                    continue
                state = live.get(module.get("id"), {"id": module.get("id")})
                cameras.append(
                    {
                        "id": module.get("id"),
                        "name": module.get("name") or CAMERA_TYPES[module["type"]],
                        "kind": CAMERA_TYPES[module["type"]],
                        "state": camera_state(state, unreachable),
                        "monitoring": state.get("monitoring"),
                    }
                )
            seen = {p.get("id"): p for p in (status.get("home") or {}).get("persons") or []}
            persons = []
            for person in home.get("persons") or []:
                if not person.get("pseudo"):
                    continue
                last = seen.get(person.get("id"), {})
                persons.append(
                    {
                        "name": person["pseudo"],
                        "home": not last.get("out_of_sight", True),
                        "last_seen": when(last.get("last_seen")),
                    }
                )
            shown.append({"home": home.get("name"), "cameras": cameras, "persons": persons})
        return {"homes": shown}

    async def get_camera_events(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        count = min(max(int(args.get("count", 10)), 1), MAX_EVENTS)
        homes = [h for h in await netatmo.homes() if _has(h, CAMERA_TYPES)]
        ctx.account_read = True
        names: dict[str, str] = {}
        cameras: dict[str, str] = {}
        found: list[dict[str, Any]] = []
        for home in homes:
            names.update({p.get("id"): p.get("pseudo") for p in home.get("persons") or []})
            cameras.update({m.get("id"): m.get("name") for m in home.get("modules") or []})
            found.extend(await netatmo.events(str(home["id"])))
        found.sort(key=lambda e: _stamp(e.get("time")) or datetime.min.replace(tzinfo=UTC))
        listed = []
        for event in reversed(found[-count:]):
            kind = str(event.get("type") or "")
            shown: dict[str, Any] = {
                "at": when(event.get("time")),
                "type": EVENT_KINDS.get(kind, kind),
            }
            device = event.get("camera_id") or event.get("device_id") or event.get("module_id")
            if cameras.get(device):
                shown["camera"] = cameras[device]
            if kind in ("person", "person_away"):
                name = names.get(event.get("person_id"))
                shown["person"] = name or "unknown face"
                if event.get("is_arrival"):
                    shown["arrival"] = True
            subtypes = sorted({str(s.get("type")) for s in event.get("subevents") or []})
            if subtypes:
                shown["detected"] = [EVENT_KINDS.get(s, s) for s in subtypes]
            if message := _text(event.get("message")):
                shown["message"] = message
            listed.append(shown)
        if not listed:
            return {"events": [], "note": "No camera event."}
        return {"events": listed}

    async def get_netatmo_measures(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        homes = await netatmo.homes()
        ctx.account_read = True
        result: dict[str, Any] = {}
        if any(_has(h, WEATHER_TYPES) for h in homes):
            result["weather_stations"] = [
                {
                    "station": station.get("home_name") or station.get("station_name"),
                    "modules": [
                        _module_readings(m, when)
                        for m in [station, *(station.get("modules") or [])]
                    ],
                }
                for station in await netatmo.stations()
            ]
        rooms = []
        for home in homes:
            if not _has(home, ENERGY_TYPES):
                continue
            named = {str(r.get("id")): r.get("name") for r in home.get("rooms") or []}
            status = await netatmo.home_status(str(home["id"]))
            for room in (status.get("home") or {}).get("rooms") or []:
                if room.get("therm_measured_temperature") is None:
                    continue
                rooms.append(
                    {
                        "home": home.get("name"),
                        "room": named.get(str(room.get("id"))) or str(room.get("id")),
                        "temperature_c": room.get("therm_measured_temperature"),
                        "setpoint_c": room.get("therm_setpoint_temperature"),
                        "mode": room.get("therm_setpoint_mode"),
                        "heating_percent": room.get("heating_power_request"),
                        "window_open": room.get("open_windows"),
                        "reachable": room.get("reachable"),
                    }
                )
        if rooms:
            result["thermostat_rooms"] = rooms
        if not result:
            return {"note": "No Netatmo weather station or thermostat on this account."}
        return result

    async def set_camera_monitoring(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        camera = await netatmo.camera(str(args.get("camera") or ""))
        on = args.get("state") == "on"
        await netatmo.set_monitoring(camera, on)
        return {"camera": camera.name, "home": camera.home, "monitoring": "on" if on else "off"}

    async def describe_monitoring(args: dict[str, Any]) -> str:
        camera = await netatmo.camera(str(args.get("camera") or ""))
        where = f'"{camera.name}" ({camera.home})' if camera.home else f'"{camera.name}"'
        if args.get("state") == "on":
            return s.camera_on_summary.format(camera=where)
        return s.camera_off_summary.format(camera=where)

    def vet_monitoring(args: dict[str, Any], _: ToolContext) -> None:
        if args.get("state") not in ("on", "off"):
            raise ToolError('state must be "on" or "off".')

    return [
        Tool(
            name="get_home_security_status",
            description=(
                "The user's Netatmo security cameras, by home: each camera's id, name, state "
                "(on, off or disconnected) and monitoring, and the known persons the cameras "
                "recognize, each at home or away, with when they were last seen. After it, no "
                "new site opens in this request."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_home_security_status,
        ),
        Tool(
            name="get_camera_events",
            description=(
                "The latest events of the user's Netatmo cameras, newest first: a known person "
                "seen (by name) or an unknown face, someone leaving, movement, camera turned on "
                "or off, connected or disconnected, with the time and the camera. count: how "
                f"many (10 by default, at most {MAX_EVENTS}). After it, no new site opens in "
                "this request."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1, "maximum": MAX_EVENTS}},
            },
            handler=get_camera_events,
        ),
        Tool(
            name="get_netatmo_measures",
            description=(
                "Current readings of the user's Netatmo weather station (each module: "
                "temperature, humidity, CO2, noise, pressure, rain, wind) and thermostat or "
                "valves (each room: temperature, setpoint, mode, heating, open window). After "
                "it, no new site opens in this request."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_netatmo_measures,
        ),
        Tool(
            name="set_camera_monitoring",
            description=(
                "Turn a Netatmo camera's monitoring on or off (off: it stops detecting, "
                "alerting and recording). camera: its id from get_home_security_status, "
                "optional when there is only one camera. The user always approves it first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "camera": {"type": "string", "description": "The camera's id."},
                    "state": {"type": "string", "enum": ["on", "off"]},
                },
                "required": ["state"],
            },
            handler=set_camera_monitoring,
            needs_clean_turn=True,
            always_propose=True,
            describe=describe_monitoring,
            vet=vet_monitoring,
        ),
    ]


def _has(home: dict[str, Any], types: set[str] | dict[str, str]) -> bool:
    return any(m.get("type") in types for m in home.get("modules") or [])


def _module_readings(module: dict[str, Any], when: Any) -> dict[str, Any]:
    data = module.get("dashboard_data") or {}
    shown: dict[str, Any] = {"name": module.get("module_name") or module.get("type")}
    if module.get("reachable") is False:
        shown["reachable"] = False
    shown.update({name: data[key] for key, name in READINGS.items() if key in data})
    if data.get("time_utc"):
        shown["at"] = when(data["time_utc"])
    if module.get("battery_percent") is not None:
        shown["battery_percent"] = module["battery_percent"]
    return shown
