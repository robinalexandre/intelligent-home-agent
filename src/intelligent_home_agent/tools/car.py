"""The user's connected car, through its maker's cloud (docs/home-devices.md).

One set of tools whatever the make (``get_car_state``, ``get_car_location``, ``lock_car``,
``unlock_car``, ``start_car_heating``, ``stop_car_heating``): a provider is a class with one
method per capability (``Car`` below), and a tool exists only when the provider says it
can serve it. Audi through myAudi is the first (tools/audi.py); ``providers()`` names
them, ``CAR_PROVIDER`` picks one.

Guardrails, enforced here whatever the provider:
- Locking and the heating act on the car: refused, or parked for the user's go, once the
  request has read others' content.
- Unlocking asks for proof only the user has (tools/unlock_codes.py): their authenticator
  code or letters of their secret phrase. Without unlock codes there is no ``unlock_car``.
  It is never parked, as the answer would be stale by the time the user says yes.
- Where the car is parked is the user's own data: after reading it, the request opens no new
  site of its own choosing (ToolContext.account_read).
- A command is reported done only once the maker confirms the car carried it out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.unlock_codes import CODE_SCHEMA, UnlockCodes

# What a command came to. Only SUCCEEDED means the car did it.
SUCCEEDED, PENDING, SENT = "succeeded", "pending", "sent"
# Capabilities a provider may have, beyond reading the car's state.
LOCATION, LOCK, UNLOCK, HEATING = "location", "lock", "unlock", "heating"
DEFAULT_HEATING_MINUTES = 30


@dataclass(frozen=True)
class CarAccount:
    """What the settings hold of the user's account at the maker (CAR_*)."""

    vin: str
    refresh_token: str | None = None
    client_id: str | None = None
    # The PIN the maker asks before a command (Audi: the S-PIN).
    pin: str | None = None
    # Where the provider keeps what changes (a rotated token), in DATA_DIR.
    store: Path = Path("car.json")


class Car:
    """A car at its maker's cloud. A provider subclasses it, sets ``vendor``, says in
    ``can`` what this car and this account allow, and writes the methods it can serve.

    ``state`` returns what the car last reported, with these keys where it knows them (and
    any other the maker has): ``locked`` (bool), ``open_doors``, ``open_windows`` (names),
    ``range_km``, ``fuel_percent``, ``battery_percent``, ``charging``, ``odometer_km``,
    ``heating`` (its state), ``online`` (bool), ``captured_at`` (an ISO timestamp).
    ``location`` returns ``latitude``, ``longitude`` and ``captured_at``, or None when the
    maker has no parked position. A command returns SUCCEEDED only when the maker confirmed
    the car carried it out, PENDING when it has not answered yet, SENT when the maker gives
    no way to follow it. Errors for the model are ``ToolError``.
    """

    # The maker's service as the user knows it: "Audi (myAudi)".
    vendor = ""
    # Shortest and longest heating the maker accepts, in minutes.
    heating_minutes = (10, 60)
    # What the heating is on this car, for the model (e.g. that it burns fuel).
    heating_note = ""

    def can(self, capability: str) -> bool:
        return False

    async def state(self) -> dict[str, Any]:
        raise NotImplementedError

    async def location(self) -> dict[str, Any] | None:
        raise NotImplementedError

    async def lock(self) -> str:
        raise NotImplementedError

    async def unlock(self) -> str:
        raise NotImplementedError

    async def start_heating(self, minutes: int) -> str:
        raise NotImplementedError

    async def stop_heating(self) -> str:
        raise NotImplementedError


def providers() -> dict[str, Callable[[httpx.AsyncClient, CarAccount], Car]]:
    """The makes spoken to, by their CAR_PROVIDER name (config.CAR_PROVIDERS lists the
    same names). A new one is a module beside tools/audi.py and a line here."""
    from intelligent_home_agent.tools.audi import connect as audi

    return {"audi": audi}


def build_car(name: str | None, http: httpx.AsyncClient, account: CarAccount | None) -> Car | None:
    if not name or account is None:
        return None
    return providers()[name](http, account)


def build_car_tools(
    car: Car | None,
    name: str = "",
    unlock_codes: UnlockCodes | None = None,
    timezone: str | None = None,
    *,
    language: str = "en",
) -> list[Tool]:
    """``name``: how the user calls their car ("the Audi"), empty for "the car"."""
    if car is None:
        return []
    s = strings(language)
    tz = ZoneInfo(timezone) if timezone else None
    shown = name or s.car_default_name
    called = f" ({name})" if name else ""
    low, high = car.heating_minutes

    def when(stamp: Any) -> Any:
        if tz is None or not isinstance(stamp, str):
            return stamp
        try:
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return stamp
        return at.astimezone(tz).isoformat(timespec="seconds") if at.tzinfo else stamp

    def result(action: str, outcome: str) -> dict[str, Any]:
        notes = {
            SUCCEEDED: f"{car.vendor} confirms the car carried it out.",
            PENDING: "The car hasn't confirmed yet (it can take a few minutes when it is "
            "asleep or the network is weak): don't say it is done; get_car_state shows "
            "whether it went through.",
            SENT: f"{car.vendor} accepted it without a way to follow it: don't say it is "
            "done; get_car_state shows whether it went through.",
        }
        return {"car": shown, "action": action, "result": outcome, "note": notes[outcome]}

    def minutes_of(args: dict[str, Any]) -> int:
        try:
            wanted = int(args.get("minutes", DEFAULT_HEATING_MINUTES))
        except (TypeError, ValueError):
            raise ToolError("minutes must be a number.") from None
        return min(max(wanted, low), high)

    async def get_car_state(_: dict[str, Any], __: ToolContext) -> dict[str, Any]:
        state = dict(await car.state())
        if "captured_at" in state:
            state["captured_at"] = when(state["captured_at"])
        return {"car": shown, **state}

    async def get_car_location(_: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        position = await car.location()
        ctx.account_read = True
        if not position or position.get("latitude") is None:
            return {
                "car": shown,
                "parked": False,
                "note": f"No parked position: the car is probably being driven ({car.vendor} "
                "only reports where it was parked once it is switched off).",
            }
        lat, lon = position["latitude"], position["longitude"]
        return {
            "car": shown,
            "parked": True,
            "latitude": lat,
            "longitude": lon,
            "captured_at": when(position.get("captured_at")),
            "map": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}",
        }

    async def lock_car(_: dict[str, Any], __: ToolContext) -> dict[str, Any]:
        return result("lock", await car.lock())

    async def unlock_car(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        if unlock_codes is None:
            raise ToolError("Unlocking the car is not set up.")
        if not str(args.get("code") or "").strip():
            return {"car": shown, "unlocked": False, "ask": unlock_codes.ask()}
        unlock_codes.check(str(args["code"]))
        return result("unlock", await car.unlock())

    async def start_car_heating(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        minutes = minutes_of(args)
        return {**result("heating_start", await car.start_heating(minutes)), "minutes": minutes}

    async def stop_car_heating(_: dict[str, Any], __: ToolContext) -> dict[str, Any]:
        return result("heating_stop", await car.stop_heating())

    async def describe_lock(_: dict[str, Any]) -> str:
        return s.car_lock_summary.format(name=shown)

    async def describe_start(args: dict[str, Any]) -> str:
        return s.car_heating_start_summary.format(name=shown, minutes=minutes_of(args))

    async def describe_stop(_: dict[str, Any]) -> str:
        return s.car_heating_stop_summary.format(name=shown)

    nothing = {"type": "object", "properties": {}}
    tools = [
        Tool(
            name="get_car_state",
            description=(
                f"The user's car{called} as {car.vendor} last heard from it: locked or not, "
                "open doors and windows, range, fuel or battery level, mileage, and what else "
                "the maker reports (service due, lights left on, the heating, whether the car "
                "is online), with when it last reported."
            ),
            input_schema=nothing,
            handler=get_car_state,
        )
    ]
    if car.can(LOCATION):
        tools.append(
            Tool(
                name="get_car_location",
                description=(
                    f"Where the user's car{called} is parked: coordinates, when, and a map "
                    "link. Nothing while it is being driven. After it, no new site opens in "
                    "this request."
                ),
                input_schema=nothing,
                handler=get_car_location,
            )
        )
    if car.can(LOCK):
        tools.append(
            Tool(
                name="lock_car",
                description=(
                    f"Lock the user's car{called} remotely, then wait up to a minute for the "
                    "car to confirm."
                ),
                input_schema=nothing,
                handler=lock_car,
                needs_clean_turn=True,
                describe=describe_lock,
            )
        )
    if car.can(HEATING):
        note = f" {car.heating_note}" if car.heating_note else ""
        tools.append(
            Tool(
                name="start_car_heating",
                description=(
                    f"Start the heating of the user's car{called} before a drive, for {low} to "
                    f"{high} minutes ({DEFAULT_HEATING_MINUTES} by default).{note} Only when "
                    "the user asks."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "minutes": {"type": "integer", "minimum": low, "maximum": high},
                    },
                },
                handler=start_car_heating,
                needs_clean_turn=True,
                describe=describe_start,
            )
        )
        tools.append(
            Tool(
                name="stop_car_heating",
                description=f"Stop the heating of the user's car{called}.",
                input_schema=nothing,
                handler=stop_car_heating,
                needs_clean_turn=True,
                describe=describe_stop,
            )
        )
    if car.can(UNLOCK) and unlock_codes is not None:
        # The answer is the user's go, and would be stale by the time a parked call ran.
        tools.append(
            Tool(
                name="unlock_car",
                description=(
                    f"Unlock the user's car{called} remotely, once they prove it is them. Call "
                    "it without `code` first: it returns the question to ask them (their "
                    "authenticator code, or letters of their secret phrase). Ask it and stop; "
                    "when they answer, call again with their answer as `code`."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"code": CODE_SCHEMA},
                },
                handler=unlock_car,
                needs_clean_turn=True,
                never_park=True,
            )
        )
    return tools
