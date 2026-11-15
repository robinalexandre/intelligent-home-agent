"""The Apple devices in the user's Find My (icloud_web), their family's included: where they
are, their battery, and a sound to find one. Lost mode and erasing are left out.

A fresh position needs the devices to answer: Find My is asked to locate them all, and the
answers that came in a few seconds later are read. Where people are is the user's own
data, not public web: after it, the request opens no new site of its own choosing, so no
address can carry it out (ToolContext.account_read).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

if TYPE_CHECKING:
    from intelligent_home_agent.icloud_web import ICloudWeb

LOCATE_WAIT_SECONDS = 6
AT_HOME_METERS = 150
_STATUSES = {"200": "online", "201": "offline", "203": "offline", "204": "unregistered"}
_CHARGING = {"Charging": "charging", "Charged": "charged", "NotCharging": "not charging"}


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    model: str
    status: str
    battery: int | None
    charging: str | None
    low_power: bool
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    located_at: datetime | None = None
    can_ring: bool = False
    # A family member's first name; None for the user's own devices.
    owner: str | None = None


class DeviceSource(Protocol):
    async def devices(self) -> list[Device]: ...

    async def known(self, device_id: str) -> Device | None: ...

    async def play_sound(self, device_id: str) -> Device: ...


def device_from(
    data: dict[str, Any], can_ring: bool, members: dict[str, Any] | None = None
) -> Device:
    status = _STATUSES.get(str(data.get("deviceStatus")), "unknown")
    level = data.get("batteryLevel")
    # Find My reports 0 when it doesn't know: only a level a device sent counts.
    known_level = isinstance(level, int | float) and (
        level > 0 or data.get("batteryStatus") in _CHARGING
    )
    location = data.get("location") or {}
    stamp = location.get("timeStamp")
    # A family member's device carries their prsId; the user's own carry none.
    owner = None
    if prs := data.get("prsId"):
        member = (members or {}).get(prs) or {}
        owner = str(member.get("firstName") or "").strip().title() or "family"
    return Device(
        id=str(data.get("id", "")),
        name=str(data.get("name") or data.get("deviceDisplayName") or "?"),
        model=str(data.get("deviceDisplayName") or ""),
        status=status,
        battery=round(level * 100) if known_level else None,
        charging=_CHARGING.get(str(data.get("batteryStatus"))),
        low_power=bool(data.get("lowPowerMode")),
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
        accuracy_m=location.get("horizontalAccuracy"),
        located_at=datetime.fromtimestamp(stamp / 1000).astimezone() if stamp else None,
        can_ring=can_ring,
        owner=owner,
    )


def _members(manager: Any) -> dict[str, Any]:
    info = manager.user_info or {}
    members = info.get("membersInfo")
    return dict(members) if isinstance(members, dict) else {}


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class FindMy:
    def __init__(self, icloud: ICloudWeb) -> None:
        self._icloud = icloud
        self._last: dict[str, Device] = {}

    async def devices(self) -> list[Device]:
        def locate(api: Any) -> list[Device]:
            manager = api.devices
            manager.refresh(locate=True)
            time.sleep(LOCATE_WAIT_SECONDS)
            manager.refresh(locate=False)
            members = _members(manager)
            return [device_from(dict(d.data), bool(d.sound_available), members) for d in manager]

        found = await self._icloud.run(locate)
        self._last = {d.id: d for d in found}
        return found

    async def known(self, device_id: str) -> Device | None:
        return self._last.get(device_id)

    async def play_sound(self, device_id: str) -> Device:
        def ring(api: Any) -> Device:
            manager = api.devices
            for item in manager:
                if str(item.data.get("id")) == device_id:
                    item.play_sound()
                    return device_from(dict(item.data), True, _members(manager))
            raise ToolError(f"No device {device_id}: find_my_devices gives the ids.")

        return await self._icloud.run(ring)


def build_find_my_tools(
    source: DeviceSource,
    timezone: str,
    home: tuple[float, float] | None = None,
    language: str = "en",
) -> list[Tool]:
    tz = ZoneInfo(timezone)
    s = strings(language)

    def shown(d: Device) -> dict[str, Any]:
        place: dict[str, Any] | None = None
        if d.latitude is not None and d.longitude is not None:
            place = {
                "latitude": round(d.latitude, 5),
                "longitude": round(d.longitude, 5),
                "accuracy_m": round(d.accuracy_m) if d.accuracy_m is not None else None,
                "at": d.located_at.astimezone(tz).isoformat(timespec="minutes")
                if d.located_at
                else None,
                "map": f"https://maps.apple.com/?ll={d.latitude:.5f},{d.longitude:.5f}",
            }
            if home is not None:
                meters = distance_m(home, (d.latitude, d.longitude))
                place["at_home"] = meters <= AT_HOME_METERS
                place["km_from_home"] = round(meters / 1000, 1)
        return {
            "id": d.id,
            "name": d.name,
            "model": d.model,
            "owner": d.owner or "the user",
            "status": d.status,
            "battery_percent": d.battery,
            "charging": d.charging,
            "low_power_mode": d.low_power,
            "location": place,
            "can_ring": d.can_ring,
        }

    def matches(d: Device, wanted: str) -> bool:
        return any(wanted in text.casefold() for text in (d.name, d.model, d.owner or ""))

    async def find_my_devices(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        wanted = str(args.get("name") or "").casefold()
        mine = not args.get("family")
        devices = [
            d
            for d in await source.devices()
            if (d.owner is None) == mine and (not wanted or matches(d, wanted))
        ]
        ctx.account_read = True
        return {"count": len(devices), "devices": [shown(d) for d in devices]}

    async def play_sound_on_device(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        device = await source.play_sound(str(args.get("id") or ""))
        return {"status": "ringing", "device": device.name, "owner": device.owner or "the user"}

    async def describe_sound(args: dict[str, Any]) -> str:
        device = await source.known(str(args.get("id") or ""))
        if device is None:
            raise ToolError(f"No device {args.get('id')}: find_my_devices gives the ids.")
        whose = s.device_owner.format(owner=device.owner) if device.owner else ""
        return s.device_ring_summary.format(name=device.name, model=device.model, whose=whose)

    return [
        Tool(
            name="find_my_devices",
            description=(
                "The user's Apple devices in Find My (iPhone, Watch, AirPods, Mac...), "
                "located now: name, model, online or not, battery level and charging, where "
                "it is (coordinates, accuracy, when, whether at home, a map link). With "
                "family: their family members' devices instead, each with its owner's first "
                "name. name keeps the devices whose name, model or owner holds it. Two "
                "devices can share a name: tell them apart by model. After it, no new site "
                "opens in this request."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": 'e.g. "iPhone", "AirPods", or a family member\'s name.',
                    },
                    "family": {
                        "type": "boolean",
                        "description": "Family members' devices rather than the user's.",
                    },
                },
            },
            handler=find_my_devices,
        ),
        Tool(
            name="play_sound_on_device",
            description=(
                "Make one of the user's devices play the Find My sound, to find it, by its id "
                "from find_my_devices (only those with can_ring)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The id find_my_devices gave."}
                },
                "required": ["id"],
            },
            handler=play_sound_on_device,
            needs_clean_turn=True,
            proposable=True,
            describe=describe_sound,
        ),
    ]
