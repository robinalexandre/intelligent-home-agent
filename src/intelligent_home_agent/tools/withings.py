"""The user's weigh-ins from their Withings scale (weight and body composition), read-only,
through Withings' public API (scripts/withings_login.py connects it once).

What the scale measured is the user's own data, not public web: after it, the request
opens no new site of its own choosing, so no address can carry it out
(ToolContext.account_read).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"  # noqa: S105 - an endpoint, not a secret
MEASURE_URL = "https://wbsapi.withings.net/measure"
SCOPES = "user.metrics"
# Withings' measure types, by the names the tool gives them.
MEASURES = {
    1: "weight_kg",
    5: "lean_mass_kg",
    6: "fat_percent",
    8: "fat_mass_kg",
    11: "heart_rate_bpm",
    76: "muscle_mass_kg",
    77: "water_kg",
    88: "bone_mass_kg",
    91: "pulse_wave_velocity_m_s",
    170: "visceral_fat_index",
    226: "basal_metabolic_rate",
}
# A weigh-in the scale could not tell apart from another user's.
_AMBIGUOUS = 1
_REAL_MEASURES = 1
_UNAUTHORIZED = 401
_RATE_LIMITED = (601, 602)
TREND_DAYS = (7, 30)
MAX_LISTED = 60


def _body(resp: httpx.Response) -> tuple[int, dict[str, Any]]:
    """Withings answers HTTP 200 with the outcome in the body: status 0 is success."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise ToolError(f"Withings sent no answer (HTTP {resp.status_code}).") from exc
    status = data.get("status")
    return (status if isinstance(status, int) else -1), data.get("body") or {}


class WithingsAuth:
    """Access tokens from the refresh token; Withings rotates it on each use, so the new one
    is saved each time and only one refresh runs at a time."""

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
                raise ToolError("Withings is not connected: run scripts/withings_login.py.")
            try:
                resp = await self._http.post(
                    TOKEN_URL,
                    data={
                        "action": "requesttoken",
                        "grant_type": "refresh_token",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                    },
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"Withings unavailable: {exc}") from exc
            status, body = _body(resp)
            if status != 0 or "access_token" not in body:
                raise ToolError(
                    f"Withings refused the login (status {status}): run "
                    "scripts/withings_login.py again."
                )
            self._access_token = body["access_token"]
            self._expires_at = self._clock() + float(body.get("expires_in", 10800))
            if body.get("refresh_token") and body["refresh_token"] != self._refresh_token:
                self._refresh_token = body["refresh_token"]
                self._save()
            return self._access_token

    def _save(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"client_id": self._client_id, "refresh_token": self._refresh_token})
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self._store)


@dataclass(frozen=True)
class WeighIn:
    at: datetime
    values: dict[str, float]


def weigh_ins_from(groups: list[dict[str, Any]]) -> list[WeighIn]:
    """One weigh-in per moment, oldest first: the scale may send its measures in several
    groups for the same weigh-in."""
    merged: dict[int, dict[str, float]] = {}
    for group in groups:
        real = group.get("category", _REAL_MEASURES) == _REAL_MEASURES
        if not real or group.get("attrib") == _AMBIGUOUS:
            continue
        values = merged.setdefault(int(group.get("date", 0)), {})
        for measure in group.get("measures", []):
            name = MEASURES.get(measure.get("type"))
            if name is not None:
                values[name] = round(measure["value"] * 10 ** measure["unit"], 2)
    return [
        WeighIn(datetime.fromtimestamp(stamp, tz=UTC), values)
        for stamp, values in sorted(merged.items())
        if values
    ]


class Withings:
    def __init__(self, http: httpx.AsyncClient, auth: WithingsAuth) -> None:
        self._http = http
        self._auth = auth

    async def weigh_ins(self, since: datetime, until: datetime) -> list[WeighIn]:
        groups: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {
                "action": "getmeas",
                "meastypes": ",".join(str(t) for t in MEASURES),
                "category": _REAL_MEASURES,
                "startdate": int(since.timestamp()),
                "enddate": int(until.timestamp()),
            }
            if offset:
                params["offset"] = offset
            body = await self._post(MEASURE_URL, params)
            groups.extend(body.get("measuregrps") or [])
            if not body.get("more"):
                return weigh_ins_from(groups)
            offset = int(body.get("offset") or 0)

    async def _post(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        status, body = await self._send(url, params, await self._auth.access_token())
        if status == _UNAUTHORIZED:
            status, body = await self._send(url, params, await self._auth.access_token(force=True))
        if status == 0:
            return body
        if status == _UNAUTHORIZED:
            raise ToolError("Withings refused the login: run scripts/withings_login.py again.")
        if status in _RATE_LIMITED:
            raise ToolError("Withings asks to wait: too many requests, try again later.")
        raise ToolError(f"Withings error (status {status}).")

    async def _send(
        self, url: str, params: dict[str, Any], token: str
    ) -> tuple[int, dict[str, Any]]:
        try:
            resp = await self._http.post(
                url, data=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Withings unavailable: {exc}") from exc
        return _body(resp)


def changes(weigh_ins: list[WeighIn], days: int) -> dict[str, Any] | None:
    """How the latest weigh-in differs from the first one of the days before it."""
    latest = weigh_ins[-1]
    start = latest.at - timedelta(days=days)
    first = next(w for w in weigh_ins if w.at >= start)
    if first is latest:
        return None
    return {
        "since": first.at.date().isoformat(),
        "weigh_ins": sum(1 for w in weigh_ins if w.at >= start),
        "change": {
            name: round(value - first.values[name], 2)
            for name, value in latest.values.items()
            if name in first.values
        },
    }


def build_withings_tools(withings: Withings, timezone: str) -> list[Tool]:
    tz = ZoneInfo(timezone)

    def shown(w: WeighIn) -> dict[str, Any]:
        return {"at": w.at.astimezone(tz).isoformat(timespec="minutes"), **w.values}

    async def get_body_measurements(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        days = min(max(int(args.get("days", 30)), 1), 365)
        now = datetime.now(tz)
        found = await withings.weigh_ins(now - timedelta(days=max(days, *TREND_DAYS)), now)
        ctx.account_read = True
        if not found:
            return {"weigh_ins": 0, "note": "No weigh-in in that period."}
        listed = [w for w in found if w.at >= now - timedelta(days=days)][-MAX_LISTED:]
        return {
            "latest": shown(found[-1]),
            **{f"last_{n}_days": changes(found, n) for n in TREND_DAYS},
            "weigh_ins": [shown(w) for w in listed],
        }

    return [
        Tool(
            name="get_body_measurements",
            description=(
                "The user's weigh-ins from their Withings scale: the latest one (weight, fat "
                "percent and mass, lean and muscle mass, water, bone mass, visceral fat "
                "index, heart rate, as the scale measured them), how each changed over the "
                "last 7 and 30 days, and every weigh-in of the period (days, 30 by default). "
                "After it, no new site opens in this request."
            ),
            input_schema={
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365}},
            },
            handler=get_body_measurements,
        )
    ]
