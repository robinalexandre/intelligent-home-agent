"""An Audi through myAudi's cloud (CARIAD): the first provider of tools/car.py.

Audi publishes no API for this: the one used here is the private API the myAudi app talks
to, as community projects worked it out. It is unofficial, may change or stop without
notice, and using it may not be allowed by Audi's terms: read them before turning it on
(docs/home-devices.md). Accounts sign in through Audi's interactive web login, once, from
a browser (scripts/audi_login.py); the agent then lives on the refresh token, which Audi
rotates, so the latest one is kept in a file.

The guardrails are those of tools/car.py. Here: a command needs the account's S-PIN
(CAR_PIN), and is reported succeeded only once Audi says the car carried it out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from intelligent_home_agent.tools.base import ToolError
from intelligent_home_agent.tools.car import (
    HEATING,
    LOCATION,
    LOCK,
    PENDING,
    SENT,
    SUCCEEDED,
    UNLOCK,
    Car,
    CarAccount,
)
from intelligent_home_agent.tools.unlock_codes import write_private

MARKETS_URL = "https://content.app.my.audi.com/service/mobileapp/configurations/markets"
MARKET_URL = "https://content.app.my.audi.com/service/mobileapp/configurations/market/{country}/{language}?v=4.23.1"
AUTHORIZE_URL = "https://group.identity.vwgroup.io/authorize"
TOKEN_URL = "https://group.identity.vwgroup.io/oauth/token"  # noqa: S105 - a URL
AUDIENCE = "https://api.cariad.com"
SCOPE = "openid profile offline_access"
# The redirect the myAudi login client accepts: nothing listens there but the login script.
REDIRECT_URI = "http://localhost:8080/myaudi-callback"
# The myAudi Android client, when the market configuration names none.
DEFAULT_CLIENT_ID = "09b6cbec-cd19-4589-82fd-363dfa8c24da@apps_vw-dilab_com"
BFF_URL = "https://emea.bff.cariad.digital"
GRAPHQL_URL = "https://app-api.live-my.audi.com/vgql/v1/graphql"
APP_VERSION = "4.31.0"
USER_AGENT = (
    f"Android/{APP_VERSION} (Build 800341641.root project 'myaudi_android'.ext.buildTime) "
    "Android/13"
)

STATUS_JOBS = (
    "access",
    "auxiliaryHeating",
    "climatisation",
    "fuelStatus",
    "measurements",
    "oilLevel",
    "readiness",
    "userCapabilities",
    "vehicleHealthInspection",
    "vehicleLights",
)
CONFIRM_ATTEMPTS = 12
CONFIRM_SLEEP_SECONDS = 5

LOGIN_AGAIN = (
    "run scripts/audi_login.py on a computer with a browser and put what it writes in .env"
)


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def discover_client_id(http: httpx.AsyncClient, country: str) -> tuple[str, str]:
    """The myAudi client id and language for a country, from Audi's market configuration."""
    try:
        markets = (await http.get(MARKETS_URL)).json()
        spec = markets["countries"]["countrySpecifications"][country.upper()]
        language = spec["defaultLanguage"]
        market = (await http.get(MARKET_URL.format(country=country, language=language))).json()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ToolError(f"Audi's market configuration is unreadable for {country}.") from exc
    return market.get("idkClientIDAndroidLive") or DEFAULT_CLIENT_ID, language


class AudiAuth:
    """Access tokens from the refresh token. Audi rotates it on each refresh, so the latest is
    saved each time and only one refresh runs at a time. A fresh login (another token in the
    settings than the one the file started from) wins over the saved one: replaying an old
    token after a new login could make the login service revoke both."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        client_id: str,
        refresh_token: str | None,
        store: Path,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._refresh_token = refresh_token
        self._configured = _digest(refresh_token) if refresh_token else None
        self._store = store
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        if store.exists():
            try:
                saved = json.loads(store.read_text())
            except ValueError:
                saved = {}
            if saved.get("configured") == self._configured and saved.get("refresh_token"):
                self._refresh_token = saved["refresh_token"]

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    async def access_token(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._access_token and self._clock() < self._expires_at - 60:
                return self._access_token
            if not self._refresh_token:
                raise ToolError(f"The Audi is not connected: {LOGIN_AGAIN}.")
            try:
                resp = await self._http.post(
                    TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "audience": AUDIENCE,
                        "scope": SCOPE,
                    },
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"myAudi unavailable: {exc}") from exc
            if resp.status_code >= 500:
                raise ToolError(f"myAudi unavailable (HTTP {resp.status_code}).")
            body = _json(resp)
            if resp.status_code != 200 or "access_token" not in body:
                raise ToolError(f"myAudi refused the login: {LOGIN_AGAIN}.")
            self._access_token = body["access_token"]
            self._expires_at = self._clock() + float(body.get("expires_in", 3600))
            if body.get("refresh_token") and body["refresh_token"] != self._refresh_token:
                self._refresh_token = body["refresh_token"]
                write_private(
                    self._store,
                    {"configured": self._configured, "refresh_token": self._refresh_token},
                )
            return self._access_token


class Audi(Car):
    vendor = "Audi (myAudi)"
    heating_minutes = (10, 60)
    heating_note = (
        "On an Audi it is the auxiliary heater, which burns fuel, and only cars whose "
        "myAudi services list it have one."
    )

    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: AudiAuth,
        vin: str,
        spin: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._auth = auth
        self.vin = vin.upper()
        self._spin = spin
        self._sleep = sleep

    def can(self, capability: str) -> bool:
        # Reading where it is parked needs nothing more; every command needs the S-PIN.
        if capability == LOCATION:
            return True
        return capability in (LOCK, UNLOCK, HEATING) and bool(self._spin)

    async def status(self) -> dict[str, Any]:
        """Audi's own answer, as the car last reported."""
        data = await self._request("GET", f"selectivestatus?jobs={','.join(STATUS_JOBS)}")
        return data if isinstance(data, dict) else {}

    async def state(self) -> dict[str, Any]:
        return shown_status(await self.status())

    async def location(self) -> dict[str, Any] | None:
        data = await self._request("GET", "parkingposition")
        position = data.get("data") if isinstance(data, dict) else None
        if not isinstance(position, dict) or position.get("lat") is None:
            return None
        return {
            "latitude": position["lat"],
            "longitude": position.get("lon"),
            "captured_at": position.get("carCapturedTimestamp"),
        }

    async def lock(self) -> str:
        return await self._command("access/lock", {"spin": self._needs_spin()}, "lock")

    async def unlock(self) -> str:
        return await self._command("access/unlock", {"spin": self._needs_spin()}, "unlock")

    async def start_heating(self, minutes: int) -> str:
        body = {"duration_min": minutes, "spin": self._needs_spin()}
        return await self._command("auxiliaryheating/start", body, "heater")

    async def stop_heating(self) -> str:
        return await self._command("auxiliaryheating/stop", None, "heater")

    def _needs_spin(self) -> str:
        if not self._spin:
            raise ToolError("No S-PIN is set (CAR_PIN): Audi needs it for this.")
        return self._spin

    async def _command(self, path: str, body: dict[str, Any] | None, what: str) -> str:
        """Send a command and follow it until the car answers: succeeded, failed or pending."""
        reply = await self._request("POST", path, json=body)
        request_id = (reply.get("data") or {}).get("requestID") if isinstance(reply, dict) else None
        if not request_id:
            return SENT
        for _ in range(CONFIRM_ATTEMPTS):
            await self._sleep(CONFIRM_SLEEP_SECONDS)
            pending = await self._request("GET", "pendingrequests")
            entries = pending.get("data") if isinstance(pending, dict) else None
            entry = next(
                (e for e in entries or [] if isinstance(e, dict) and e.get("id") == request_id),
                None,
            )
            state = (entry or {}).get("status")
            if state == "successful":
                return SUCCEEDED
            if state and state != "in_progress":
                raise ToolError(f"The car refused the {what} command ({state}).")
        return PENDING

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        url = f"{BFF_URL}/vehicle/v1/vehicles/{self.vin}/{path}"
        resp = await self._send(method, url, json, force=False)
        if resp.status_code == 401:
            resp = await self._send(method, url, json, force=True)
        status = resp.status_code
        if status == 401:
            raise ToolError(f"myAudi refused the session: {LOGIN_AGAIN}.")
        if status == 403:
            raise ToolError(
                "Audi refused: this car, its myAudi services or this account don't allow it "
                "(a wrong S-PIN gives this too)."
            )
        if status == 404:
            raise ToolError("Audi doesn't offer this for this car.")
        if status == 429:
            raise ToolError("Audi asks to wait: too many requests, try again in a few minutes.")
        if status >= 400:
            raise ToolError(f"myAudi error (HTTP {status}).")
        if status == 204 or not resp.content:
            return None
        return _json(resp)

    async def _send(self, method: str, url: str, body: Any, *, force: bool) -> httpx.Response:
        token = await self._auth.access_token(force=force)
        try:
            return await self._http.request(
                method,
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"myAudi unavailable: {exc}") from exc


def connect(http: httpx.AsyncClient, account: CarAccount) -> Audi:
    """The Audi of the settings (tools/car.py's providers)."""
    auth = AudiAuth(
        http, account.client_id or DEFAULT_CLIENT_ID, account.refresh_token, account.store
    )
    return Audi(http, auth, account.vin, account.pin)


async def list_vehicles(http: httpx.AsyncClient, access_token: str) -> list[dict[str, Any]]:
    """The account's cars (vin, nickname, model), for the login script."""
    query = "query vehicleList { userVehicles { vin nickname vehicle { media { shortName } } } }"
    resp = await http.post(
        GRAPHQL_URL,
        json={"query": query},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "X-App-Name": "myAudi",
            "X-App-Version": APP_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    data = (_json(resp).get("data") or {}) if resp.status_code == 200 else {}
    return [v for v in data.get("userVehicles") or [] if isinstance(v, dict) and v.get("vin")]


def _value(data: dict[str, Any], job: str, block: str) -> dict[str, Any]:
    value = ((data.get(job) or {}).get(block) or {}).get("value")
    return value if isinstance(value, dict) else {}


def _named(items: Any, state: str) -> list[str]:
    return [
        str(item.get("name"))
        for item in items or []
        if isinstance(item, dict) and state in (item.get("status") or [])
    ]


def shown_status(data: dict[str, Any]) -> dict[str, Any]:
    """Audi's status in the words of tools/car.py, what the car doesn't report left out."""
    access = _value(data, "access", "accessStatus")
    rng = _value(data, "fuelStatus", "rangeStatus")
    fuel = _value(data, "measurements", "fuelLevelStatus")
    odometer = _value(data, "measurements", "odometerStatus")
    adblue = _value(data, "measurements", "rangeStatus")
    service = _value(data, "vehicleHealthInspection", "maintenanceStatus")
    oil = _value(data, "oilLevel", "oilLevelStatus")
    lights = _value(data, "vehicleLights", "lightsStatus")
    heater = _value(data, "auxiliaryHeating", "auxiliaryHeatingStatus") or _value(
        data, "climatisation", "auxiliaryHeatingStatus"
    )
    readiness = _value(data, "readiness", "readinessStatus").get("connectionState") or {}
    engine = rng.get("primaryEngine") or {}
    shown: dict[str, Any] = {
        "locked": {"locked": True, "unlocked": False}.get(access.get("doorLockStatus")),
        "open_doors": _named(access.get("doors"), "open"),
        "open_windows": _named(access.get("windows"), "open"),
        "range_km": rng.get("totalRange_km", engine.get("remainingRange_km")),
        "fuel_percent": fuel.get("currentFuelLevel_pct", engine.get("currentFuelLevel_pct")),
        "adblue_range_km": adblue.get("adBlueRange"),
        "odometer_km": odometer.get("odometer"),
        "inspection_due_days": service.get("inspectionDue_days"),
        "inspection_due_km": service.get("inspectionDue_km"),
        "oil_service_due_days": service.get("oilServiceDue_days"),
        "oil_service_due_km": service.get("oilServiceDue_km"),
        "oil_level": oil.get("value"),
        "lights_on": _named(lights.get("lights"), "on") or None,
        "heating": heater.get("climatisationState"),
        "heating_minutes_left": heater.get("remainingClimatisationTime_min"),
        "online": readiness.get("isOnline"),
        "captured_at": access.get("carCapturedTimestamp") or rng.get("carCapturedTimestamp"),
    }
    services = ((data.get("userCapabilities") or {}).get("capabilitiesStatus") or {}).get("value")
    if isinstance(services, list):
        # What the car's myAudi services offer: whether it has the heater, for one.
        shown["services"] = sorted(
            str(c["id"]) for c in services if isinstance(c, dict) and c.get("id")
        )
    return {k: v for k, v in shown.items() if v not in (None, [])}
