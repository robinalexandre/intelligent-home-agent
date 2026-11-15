"""The user's PetKit feeder and litter box, through PetKit's cloud (no official API: the
signed calls of the phone app, as pypetkitapi makes them).

What the devices report (meals, the pet's visits) is the user's own data: after it, the
request opens no new site of its own choosing (ToolContext.account_read). Feeding and
cleaning act on the home, so they need a clean request and show the user exactly what
they do.

PetKit keeps one session per account: logging in here logs the phone app out, and the
other way round. The devices should be shared with a second PetKit account used only here.

Needs the ``petkit`` extra (pypetkitapi, pinned: PetKit changes its API without notice);
without it the feature stays off (home_devices.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from pypetkitapi import (
    D3,
    D4,
    D4H,
    D4S,
    D4SH,
    FEEDER,
    FEEDER_MINI,
    DeviceAction,
    DeviceCommand,
    Feeder,
    FeederCommand,
    LBCommand,
    Litter,
    PetkitAuthenticationUnregisteredEmailError,
    PetKitClient,
    PetkitRegionalServerNotFoundError,
    PetkitSessionExpiredError,
    PetkitTimeoutError,
    PypetkitError,
)
from pypetkitapi.exceptions import PetkitAuthenticationError, PetkitServerBusyError

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_PORTIONS = 3
MAX_LISTED = 10
# How a manual meal is asked of each feeder: the amount key and what one portion is
# worth. D4 and D4H take grams in steps of 10, the others a number of portions.
PORTION = {
    D4: ("amount", 10),
    D4H: ("amount", 10),
    FEEDER: ("amount", 1),
    D4S: ("amount1", 1),
    D4SH: ("amount1", 1),
}
IN_GRAMS = {D3, D4, D4H, FEEDER_MINI}
# Litter box record types (as the Home Assistant integration reads them).
_CLEANING = 5
_PET_VISIT = 10
CLEANING_RESULTS = {0: "completed", 1: "stopped", 2: "failed", 3: "cancelled", 4: "cancelled"}
CLEANING_STARTS = {0: "auto", 1: "scheduled", 2: "manual", 3: "manual"}

ClientFactory = Callable[[], Awaitable[Any]]


def _unit(device_type: str) -> str:
    return "g" if device_type in IN_GRAMS else "portions"


def _clock(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    return f"{seconds // 3600 % 24:02d}:{seconds // 60 % 60:02d}"


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def _name(device: Feeder | Litter) -> str:
    nfo = device.device_nfo
    return device.name or (nfo.device_name if nfo else None) or str(device.id)


def _type(device: Feeder | Litter) -> str:
    return device.device_nfo.device_type if device.device_nfo else ""


def feeder_status(feeder: Feeder, now: datetime) -> dict[str, Any]:
    """Food level, today's meals and the next planned one; feed item times are seconds
    since midnight on the feeder's clock."""
    state = feeder.state
    device_type = _type(feeder)
    if state is None:
        food_low = None
    elif device_type in (D4S, D4SH):
        food_low = state.food1 == 0 or state.food2 == 0
    elif device_type == D3:
        food_low = state.food is not None and state.food < 2
    else:
        food_low = state.food == 0 if state.food is not None else None
    items = [
        item
        for day in (feeder.device_records.feed or [] if feeder.device_records else [])
        for item in day.items or []
    ]
    items.sort(key=lambda item: item.time or 0)
    done = [item for item in items if item.state is not None]
    seconds_now = now.hour * 3600 + now.minute * 60 + now.second
    upcoming = [item for item in items if item.state is None and (item.time or 0) > seconds_now]
    totals = state.feed_state if state else None

    def meal(item: Any) -> dict[str, Any]:
        amount = item.amount if item.amount is not None else item.amount1
        given = item.state.real_amount if item.state else None
        error = item.state.err_msg if item.state else None
        return _compact(
            {
                "at": _clock(item.time),
                "amount": amount,
                "dispensed": given,
                "manual": item.src not in (None, 1) or None,
                "error": error or None,
            }
        )

    return _compact(
        {
            "name": _name(feeder),
            "model": device_type,
            "unit": _unit(device_type),
            "food_low": food_low,
            "food_left_percent": state.percent if state else None,
            "feeding_now": bool(state.feeding) if state and state.feeding is not None else None,
            "desiccant_days_left": state.desiccant_left_days if state else None,
            "error": (state.error_msg or None) if state else None,
            "dispensed_today": totals.real_amount_total if totals else None,
            "manual_today": totals.add_amount_total if totals else None,
            "meals_today": [meal(item) for item in done][-MAX_LISTED:],
            "next_meal": meal(upcoming[0]) if upcoming else "none left today",
        }
    )


def litter_status(litter: Litter, tz: ZoneInfo) -> dict[str, Any]:
    state = litter.state

    def at(stamp: int | None) -> str | None:
        if not stamp:
            return None
        return datetime.fromtimestamp(stamp, tz).isoformat(timespec="minutes")

    visits: list[dict[str, Any]] = []
    cleanings: dict[int, dict[str, Any]] = {}
    for record in litter.device_records or []:
        if record.event_type == _PET_VISIT:
            content = record.content
            seconds = (
                content.time_out - content.time_in
                if content and content.time_in and content.time_out
                else None
            )
            visits.append(
                _compact({"at": at(record.timestamp), "pet": record.pet_name, "seconds": seconds})
            )
        # A cleaning shows up on its own or inside a visit's record, once the rake ran.
        for event in [record, *(record.sub_content or [])]:
            if event.event_type != _CLEANING or not event.timestamp:
                continue
            content = event.content
            cleanings[event.timestamp] = _compact(
                {
                    "at": at(event.timestamp),
                    "result": CLEANING_RESULTS.get(getattr(content, "result", None)),
                    "started": CLEANING_STARTS.get(getattr(content, "start_reason", None)),
                }
            )
    work = state.work_state if state else None
    return _compact(
        {
            "name": _name(litter),
            "model": _type(litter),
            "litter_percent": state.sand_percent if state else None,
            "litter_low": state.sand_lack if state else None,
            "waste_bin_full": state.box_full if state else None,
            "deodorant_days_left": state.deodorant_left_days if state else None,
            "working_now": work is not None and work.work_mode is not None,
            "error": (state.error_msg or None) if state else None,
            "uses_today": litter.device_stats.times if litter.device_stats else len(visits),
            "last_uses_today": sorted(visits, key=lambda v: v.get("at", ""))[-MAX_LISTED:],
            "last_cleaning_today": cleanings[max(cleanings)] if cleanings else None,
        }
    )


class PetKit:
    """The PetKit cloud through pypetkitapi, one call at a time: its client keeps the
    devices' state in memory and refreshes it on each call."""

    def __init__(
        self,
        username: str,
        password: str,
        region: str,
        timezone: str,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._region = region
        self.timezone = timezone
        self._factory = client_factory or self._new_client
        self._client: Any = None
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def _new_client(self) -> PetKitClient:
        self._session = aiohttp.ClientSession()
        return PetKitClient(
            self._username, self._password, self._region, self.timezone, session=self._session
        )

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def status(self) -> dict[str, Any]:
        tz = ZoneInfo(self.timezone)
        async with self._lock:
            feeders, litters = await self._devices()
            now = datetime.now(tz)
            return {
                "feeders": [feeder_status(f, now) for f in feeders],
                "litter_boxes": [litter_status(lb, tz) for lb in litters],
            }

    async def feed(self, portions: int, name: str | None = None) -> dict[str, Any]:
        async with self._lock:
            feeders, _ = await self._devices()
            feeder = _pick(feeders, name, "feeder")
            device_type = _type(feeder)
            if device_type not in PORTION:
                raise ToolError(f"Manual feeding is not supported on a PetKit {device_type}.")
            key, per_portion = PORTION[device_type]
            amount = portions * per_portion
            await self._command(feeder.id, FeederCommand.MANUAL_FEED, {key: amount})
            return {
                "done": True,
                "feeder": _name(feeder),
                "portions": portions,
                "amount": amount,
                "unit": _unit(device_type),
            }

    async def clean(self, name: str | None = None) -> dict[str, Any]:
        async with self._lock:
            _, litters = await self._devices()
            litter = _pick(litters, name, "litter box")
            work = litter.state.work_state if litter.state else None
            if work is not None and work.work_mode is not None:
                raise ToolError(f"{_name(litter)} is already working: try again in a few minutes.")
            await self._command(
                litter.id, DeviceCommand.CONTROL_DEVICE, {DeviceAction.START: LBCommand.CLEANING}
            )
            return {"done": True, "litter_box": _name(litter), "started": "cleaning cycle"}

    async def _devices(self) -> tuple[list[Feeder], list[Litter]]:
        client = await self._get_client()
        await self._call(client.get_devices_data)
        # The library swallows each device's errors; an expired session (the phone app
        # logged in with this account) only shows as its session being dropped, so the
        # data would be stale: fetch once more with a fresh login.
        if getattr(client, "_session", True) is None:
            await self._call(client.get_devices_data)
        entities = list(client.petkit_entities.values())
        feeders = [e for e in entities if isinstance(e, Feeder)]
        litters = [e for e in entities if isinstance(e, Litter)]
        if not feeders and not litters:
            raise ToolError("PetKit lists no feeder or litter box on this account.")
        return feeders, litters

    async def _command(self, device_id: int, action: Any, setting: Any) -> None:
        client = await self._get_client()
        try:
            sent = await self._call(lambda: client.send_api_request(device_id, action, setting))
        except ValueError as exc:
            raise ToolError(f"PetKit refused the amount: {exc}") from exc
        if not sent:
            raise ToolError("PetKit does not support that command on this device.")

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client = await self._factory()
        return self._client

    async def _call(self, call: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in range(2):
            try:
                return await call()
            except PetkitSessionExpiredError as exc:
                if attempt:
                    raise ToolError(
                        "PetKit keeps closing the session: the phone app is logged in with the "
                        "same account. Share the devices with a second PetKit account for the "
                        "assistant."
                    ) from exc
            except (PetkitAuthenticationError, PetkitAuthenticationUnregisteredEmailError) as exc:
                raise ToolError(
                    "PetKit refused the login: check PETKIT_USERNAME, PETKIT_PASSWORD and "
                    "PETKIT_REGION."
                ) from exc
            except PetkitRegionalServerNotFoundError as exc:
                raise ToolError(f"PetKit has no server for region {self._region!r}.") from exc
            except (PetkitTimeoutError, PetkitServerBusyError, aiohttp.ClientError) as exc:
                raise ToolError(f"PetKit unavailable: {exc}") from exc
            except PypetkitError as exc:
                raise ToolError(f"PetKit error: {exc}") from exc
        raise AssertionError("unreachable")


def _pick[T: (Feeder, Litter)](devices: list[T], name: str | None, kind: str) -> T:
    if name:
        wanted = name.strip().casefold()
        found = [d for d in devices if _name(d).casefold() == wanted]
        if not found:
            names = ", ".join(_name(d) for d in devices) or "none"
            raise ToolError(f"No PetKit {kind} named {name!r} (found: {names}).")
        return found[0]
    if not devices:
        raise ToolError(f"No PetKit {kind} on this account.")
    if len(devices) > 1:
        names = ", ".join(_name(d) for d in devices)
        raise ToolError(f"Several PetKit {kind}es: say which one ({names}).")
    return devices[0]


def _portions(args: dict[str, Any]) -> int:
    try:
        portions = int(args.get("portions", 1))
    except (TypeError, ValueError) as exc:
        raise ToolError("portions must be a whole number.") from exc
    if not 1 <= portions <= MAX_PORTIONS:
        raise ToolError(f"Between 1 and {MAX_PORTIONS} portions at a time.")
    return portions


def _device(args: dict[str, Any]) -> str | None:
    name = str(args.get("device") or "").strip()
    return name or None


def build_petkit_tools(petkit: PetKit, *, language: str = "en") -> list[Tool]:
    s = strings(language)

    async def get_pet_devices_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        result = await petkit.status()
        ctx.account_read = True
        return result

    async def feed_pet_now(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await petkit.feed(_portions(args), _device(args))

    async def clean_litter_box_now(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await petkit.clean(_device(args))

    async def describe_feed(args: dict[str, Any]) -> str:
        portions = _portions(args)
        which = f' "{_device(args)}"' if _device(args) else ""
        return s.pet_feed_summary.format(device=which, portions=portions)

    async def describe_clean(args: dict[str, Any]) -> str:
        which = f' "{_device(args)}"' if _device(args) else ""
        return s.pet_clean_summary.format(device=which)

    def vet_feed(args: dict[str, Any], ctx: ToolContext) -> None:
        _portions(args)

    device = {
        "type": "string",
        "description": "The device's name, only when there are several.",
    }
    return [
        Tool(
            name="get_pet_devices_status",
            description=(
                "The PetKit feeder and litter box: food level and low-food warning, the "
                "meals dispensed today (time, amount, manual or planned), the next planned "
                "meal, desiccant days left; litter level, waste bin full, the pet's visits "
                "today, the last cleaning today, errors. After it, no new site opens in "
                "this request."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_pet_devices_status,
        ),
        Tool(
            name="feed_pet_now",
            description=(
                f"Dispense a meal now from the PetKit feeder, 1 to {MAX_PORTIONS} portions "
                "(a D4 portion is 10 g), on top of the planned meals."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "portions": {"type": "integer", "minimum": 1, "maximum": MAX_PORTIONS},
                    "device": device,
                },
                "required": ["portions"],
            },
            handler=feed_pet_now,
            needs_clean_turn=True,
            describe=describe_feed,
            vet=vet_feed,
        ),
        Tool(
            name="clean_litter_box_now",
            description=(
                "Start a cleaning cycle on the PetKit litter box now (refused while it is "
                "already working)."
            ),
            input_schema={"type": "object", "properties": {"device": device}},
            handler=clean_litter_box_now,
            needs_clean_turn=True,
            describe=describe_clean,
        ),
    ]
