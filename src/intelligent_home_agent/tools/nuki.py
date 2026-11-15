"""The user's Nuki smart locks, through the Nuki Web API (api.nuki.io): the agent runs on a
server, so it goes through Nuki's cloud and the Bridge rather than the Bridge's local API.

Guardrails, enforced in code:
- Locking acts on the home: refused, or parked for the user's go, once the request has
  read others' content.
- Unlocking (and unlatching) asks for proof only the user has when unlock codes are set up
  (tools/unlock_codes.py), and is then never parked; without them it is always parked, even
  in a clean request: the user sees one line saying which door opens and approves it in a
  later message. Either way the model can never open a door on its own.
- Only smart locks are driven; an Opener or a Box is listed but never acted on, as the same
  action codes mean other things there.
- Who opened the door and when is the user's own data: after reading it, the request opens
  no new site of its own choosing (ToolContext.account_read).

The lock must be reachable from Nuki's cloud (a Bridge, or a lock with Wi-Fi or Thread
built in). A lock of another brand goes through Home Assistant.
"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.unlock_codes import CODE_SCHEMA, UnlockCodes

API_URL = "https://api.nuki.io"
# Smart Lock 1/2, Smartdoor, Smart Lock 3/4, Smart Lock 5: the same lock actions and states.
SMART_LOCK_TYPES = {0, 3, 4, 5}
_OPENER = 2
DEVICE_TYPES = {
    0: "smart_lock",
    1: "box",
    2: "opener",
    3: "smartdoor",
    4: "smart_lock",
    5: "smart_lock",
}
UNLOCK, LOCK, UNLATCH = 1, 2, 3
LOCK_STATES = {
    0: "uncalibrated",
    1: "locked",
    2: "unlocking",
    3: "unlocked",
    4: "locking",
    5: "unlatched",
    6: "unlocked_lock_n_go",
    7: "unlatching",
    224: "error_wrong_entry_code",
    225: "error_wrong_fingerprint",
    254: "motor_blocked",
    255: "undefined",
}
OPENER_STATES = {
    0: "untrained",
    1: "online",
    3: "ring_to_open_active",
    5: "open",
    7: "opening",
    253: "boot_run",
    255: "undefined",
}
_NO_DOOR_SENSOR = 0
DOOR_STATES = {
    1: "deactivated",
    2: "closed",
    3: "open",
    4: "unknown",
    5: "calibrating",
    16: "uncalibrated",
    240: "removed",
    255: "unknown",
}
_SERVER_OK = 0
_OFFLINE = 4
SERVER_STATES = {1: "unregistered", 2: "auth_uuid_invalid", 3: "auth_invalid", 4: "offline"}
LOG_ACTIONS = {
    1: "unlock",
    2: "lock",
    3: "unlatch",
    4: "lock_n_go",
    5: "lock_n_go_with_unlatch",
    11: "reset_to_default",
    208: "door_ajar_warning",
    209: "door_status_mismatch",
    224: "doorbell",
    240: "door_opened",
    241: "door_closed",
    242: "door_sensor_jammed",
    243: "firmware_update",
    250: "door_log_enabled",
    251: "door_log_disabled",
    252: "initialization",
    253: "calibration",
    254: "log_enabled",
    255: "log_disabled",
}
LOG_TRIGGERS = {
    0: "system",
    1: "manual",
    2: "button",
    3: "automatic",
    4: "web",
    5: "app",
    6: "auto_lock",
    7: "accessory",
    253: "keypad_error",
    254: "nuki_mode",
    255: "keypad",
}
_KEYPAD_ERROR = 253
LOG_RESULTS = {
    0: "success",
    1: "motor_blocked",
    2: "canceled",
    3: "too_recent",
    4: "busy",
    5: "low_motor_voltage",
    6: "clutch_failure",
    7: "motor_power_failure",
    8: "incomplete",
    9: "rejected",
    10: "rejected_night_mode",
    224: "invalid_code",
    225: "invalid_fingerprint",
    226: "invalid_nfc_tag",
    254: "other_error",
    255: "unknown_error",
}
LOG_SOURCES = {1: "keypad_code", 2: "fingerprint", 3: "tap_to_unlock"}
MAX_LOG_ENTRIES = 50
# Nuki's cloud learns the new state from the Bridge some seconds after an action: the
# command waits for it, so the reply can say the door turned rather than ask to check later.
SETTLE_POLLS = 6
SETTLE_SECONDS = 2.5
_LOCKED, _UNLOCKED, _UNLATCHED = 1, 3, 5
SETTLED_STATES = {LOCK: {_LOCKED}, UNLOCK: {_UNLOCKED}, UNLATCH: {_UNLOCKED, _UNLATCHED}}
_HOW_TO_RENEW = "generate a new API token at web.nuki.io > API and put it in NUKI_API_TOKEN."


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(ch for ch in decomposed if not unicodedata.combining(ch)).split())


def _label(table: dict[int, str], code: Any) -> str:
    return table.get(code, f"code_{code}") if isinstance(code, int) else "unknown"


class Nuki:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token

    async def smartlocks(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/smartlock")
        return data if isinstance(data, list) else []

    async def action(self, smartlock_id: int, action: int) -> None:
        await self._request("POST", f"/smartlock/{smartlock_id}/action", json={"action": action})

    async def logs(self, smartlock_id: int | None, limit: int) -> list[dict[str, Any]]:
        path = f"/smartlock/{smartlock_id}/log" if smartlock_id is not None else "/smartlock/log"
        data = await self._request("GET", path, params={"limit": limit})
        return data if isinstance(data, list) else []

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(
                method,
                API_URL + path,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Nuki unavailable: {exc}") from exc
        if resp.status_code == 401:
            raise ToolError(f"Nuki refused the API token: {_HOW_TO_RENEW}")
        if resp.status_code == 403:
            raise ToolError(
                "The Nuki API token lacks the permission for this: with the smartlock, action "
                f"and log scopes ticked, {_HOW_TO_RENEW}"
            )
        if resp.status_code == 402:
            raise ToolError("Nuki refused: the account needs a paid subscription for this.")
        if resp.status_code == 405:
            raise ToolError("Nuki doesn't allow this action on this device.")
        if resp.status_code == 429:
            raise ToolError("Nuki asks to wait: too many requests, try again later.")
        if resp.status_code >= 400:
            raise ToolError(f"Nuki error (HTTP {resp.status_code}).")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ToolError(f"Nuki sent no readable answer (HTTP {resp.status_code}).") from exc


def _find(locks: list[dict[str, Any]], wanted: str | None) -> dict[str, Any]:
    """One lock by id or name (accents and case ignored, a unique part of the name will do);
    with no name, the only lock there is."""
    if not locks:
        raise ToolError("No lock in this Nuki account.")
    names = ", ".join(f"'{lock.get('name')}'" for lock in locks)
    if not wanted or not str(wanted).strip():
        if len(locks) == 1:
            return locks[0]
        raise ToolError(f"Several locks: say which one ({names}).")
    wanted = str(wanted).strip()
    for lock in locks:
        if str(lock.get("smartlockId")) == wanted:
            return lock
    key = _normalize(wanted)
    exact = [lock for lock in locks if _normalize(str(lock.get("name", ""))) == key]
    if len(exact) == 1:
        return exact[0]
    partial = [lock for lock in locks if key in _normalize(str(lock.get("name", "")))]
    if len(partial) == 1:
        return partial[0]
    if partial or exact:
        raise ToolError(f"Several locks match '{wanted}': say which one ({names}).")
    raise ToolError(f"No lock named '{wanted}'. The locks: {names}.")


def _drivable(lock: dict[str, Any]) -> dict[str, Any]:
    if lock.get("type") not in SMART_LOCK_TYPES:
        kind = _label(DEVICE_TYPES, lock.get("type"))
        raise ToolError(f"'{lock.get('name')}' is a Nuki {kind}: only smart locks are driven.")
    if lock.get("serverState") == _OFFLINE:
        raise ToolError(
            f"'{lock.get('name')}' is offline for Nuki: check the Bridge's power and Wi-Fi."
        )
    return lock


def _shown_lock(lock: dict[str, Any]) -> dict[str, Any]:
    state = lock.get("state") or {}
    kind = lock.get("type")
    shown: dict[str, Any] = {
        "id": lock.get("smartlockId"),
        "name": lock.get("name"),
        "device": _label(DEVICE_TYPES, kind),
    }
    if kind in SMART_LOCK_TYPES:
        shown["state"] = _label(LOCK_STATES, state.get("state"))
    elif kind == _OPENER:
        shown["state"] = _label(OPENER_STATES, state.get("state"))
    door = state.get("doorState")
    if isinstance(door, int) and door != _NO_DOOR_SENSOR:
        shown["door"] = _label(DOOR_STATES, door)
    if state.get("batteryCritical") is not None:
        shown["battery_critical"] = bool(state["batteryCritical"])
    if isinstance(state.get("batteryCharge"), int):
        shown["battery_percent"] = state["batteryCharge"]
    if state.get("batteryCharging"):
        shown["battery_charging"] = True
    if state.get("doorsensorBatteryCritical"):
        shown["door_sensor_battery_critical"] = True
    if state.get("keypadBatteryCritical"):
        shown["keypad_battery_critical"] = True
    if state.get("nightMode"):
        shown["night_mode"] = True
    if lock.get("serverState", _SERVER_OK) != _SERVER_OK:
        shown["connection"] = _label(SERVER_STATES, lock.get("serverState"))
    if lock.get("updateDate"):
        shown["updated_at"] = lock["updateDate"]
    return shown


def build_nuki_tools(
    nuki: Nuki,
    timezone: str | None = None,
    unlock_codes: UnlockCodes | None = None,
    *,
    language: str = "en",
    wait: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> list[Tool]:
    tz = ZoneInfo(timezone) if timezone else None
    s = strings(language)

    def when(stamp: Any) -> Any:
        if tz is None or not isinstance(stamp, str):
            return stamp
        try:
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return stamp
        return at.astimezone(tz).isoformat(timespec="seconds") if at.tzinfo else stamp

    async def lock_named(args: dict[str, Any]) -> dict[str, Any]:
        return _find(await nuki.smartlocks(), args.get("lock"))

    async def settled(before: dict[str, Any], action: int) -> dict[str, Any]:
        """Waits until Nuki reports the state the action leads to, updated since it was
        sent: a lock already locked counts at once, an unlatch always needs a fresh update."""
        wanted = SETTLED_STATES[action]
        stamp = before.get("updateDate")
        already = action == LOCK and (before.get("state") or {}).get("state") in wanted
        now = before
        for _ in range(SETTLE_POLLS):
            await wait(SETTLE_SECONDS)
            found = [
                lock
                for lock in await nuki.smartlocks()
                if lock.get("smartlockId") == before["smartlockId"]
            ]
            if not found:
                break
            now = found[0]
            fresh = now.get("updateDate") != stamp or already
            if fresh and (now.get("state") or {}).get("state") in wanted:
                return {"confirmed": True, "state": _label(LOCK_STATES, now["state"]["state"])}
        shown = _shown_lock(now)
        return {
            "confirmed": False,
            "state": shown.get("state"),
            "updated_at": when(shown.get("updated_at")),
            "note": "Nuki has not reported the new state yet: get_lock_activity shows whether "
            "the lock ran the command.",
        }

    async def get_lock_state(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        locks = await nuki.smartlocks()
        if args.get("name"):
            key = _normalize(str(args["name"]))
            locks = [lock for lock in locks if key in _normalize(str(lock.get("name", "")))]
        shown = []
        for lock in locks:
            entry = _shown_lock(lock)
            if "updated_at" in entry:
                entry["updated_at"] = when(entry["updated_at"])
            shown.append(entry)
        if not shown:
            return {"locks": [], "note": "No Nuki lock matches."}
        return {"locks": shown}

    async def lock_door(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        lock = _drivable(await lock_named(args))
        await nuki.action(lock["smartlockId"], LOCK)
        return {"sent": "lock", "lock": lock.get("name"), **await settled(lock, LOCK)}

    async def unlock_door(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        lock = _drivable(await lock_named(args))
        if unlock_codes is not None:
            if not str(args.get("code") or "").strip():
                return {"lock": lock.get("name"), "unlocked": False, "ask": unlock_codes.ask()}
            unlock_codes.check(str(args["code"]))
        action = UNLATCH if args.get("open") else UNLOCK
        await nuki.action(lock["smartlockId"], action)
        return {
            "sent": "unlatch" if action == UNLATCH else "unlock",
            "lock": lock.get("name"),
            **await settled(lock, action),
        }

    async def describe_lock(args: dict[str, Any]) -> str:
        lock = _drivable(await lock_named(args))
        return s.lock_door_summary.format(name=lock.get("name"))

    async def describe_unlock(args: dict[str, Any]) -> str:
        lock = _drivable(await lock_named(args))
        if args.get("open"):
            return s.unlatch_door_summary.format(name=lock.get("name"))
        return s.unlock_door_summary.format(name=lock.get("name"))

    async def get_lock_activity(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        limit = min(max(int(args.get("limit", 10)), 1), MAX_LOG_ENTRIES)
        locks = await nuki.smartlocks()
        chosen = _find(locks, args["lock"])["smartlockId"] if args.get("lock") else None
        entries = await nuki.logs(chosen, limit)
        ctx.account_read = True
        names = {lock.get("smartlockId"): lock.get("name") for lock in locks}
        shown = []
        for entry in entries[:limit]:
            item: dict[str, Any] = {
                "at": when(entry.get("date")),
                "lock": names.get(entry.get("smartlockId"), entry.get("smartlockId")),
                "action": _label(LOG_ACTIONS, entry.get("action")),
                "by": entry.get("name") or None,
                "trigger": _label(LOG_TRIGGERS, entry.get("trigger")),
            }
            if entry.get("trigger") == _KEYPAD_ERROR:
                item["result"] = "failed"
            else:
                item["result"] = _label(LOG_RESULTS, entry.get("state"))
            if entry.get("source") in LOG_SOURCES:
                item["via"] = LOG_SOURCES[entry["source"]]
            if entry.get("autoUnlock"):
                item["auto_unlock"] = True
            shown.append({k: v for k, v in item.items() if v is not None})
        return {"entries": shown} if shown else {"entries": [], "note": "No activity logged."}

    lock_arg = {
        "lock": {
            "type": "string",
            "description": "The lock's name or id; may be left out when there is only one.",
        }
    }
    unlock_args = {
        **lock_arg,
        "open": {
            "type": "boolean",
            "description": "Also pull the latch (unlatch), not just unlock.",
        },
    }
    if unlock_codes is None:
        unlock_tool = Tool(
            name="unlock_door",
            description=(
                "Unlock one Nuki smart lock (by name or id); open=true also pulls the latch so "
                "the door swings open. Never runs at once: the user always approves it in a "
                "later message, so tell them it waits for their go."
            ),
            input_schema={"type": "object", "properties": unlock_args},
            handler=unlock_door,
            needs_clean_turn=True,
            always_propose=True,
            describe=describe_unlock,
        )
    else:
        # The answer is the user's go, and would be stale by the time a parked call ran.
        unlock_tool = Tool(
            name="unlock_door",
            description=(
                "Unlock one Nuki smart lock (by name or id); open=true also pulls the latch so "
                "the door swings open; it waits up to 15 s for the lock to report it "
                "(confirmed). Only once the user proves it is them: call it without "
                "`code` first, it returns the question to ask them (their authenticator code, "
                "or letters of their secret phrase). Ask it and stop; when they answer, call "
                "again with the same lock and open, and their answer as `code`."
            ),
            input_schema={
                "type": "object",
                "properties": {**unlock_args, "code": CODE_SCHEMA},
            },
            handler=unlock_door,
            needs_clean_turn=True,
            never_park=True,
        )
    return [
        Tool(
            name="get_lock_state",
            description=(
                "The user's Nuki smart locks: for each, its state (locked, unlocked, unlatched, "
                "locking, motor_blocked...), the door sensor (open/closed) when there is one, "
                "the battery (critical, percent), whether it is reachable, and when Nuki last "
                "updated it. Optional name filter (part of the lock's name)."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            handler=get_lock_state,
        ),
        Tool(
            name="lock_door",
            description=(
                "Lock one Nuki smart lock (by name or id). The command goes through Nuki's "
                "cloud and the Bridge, and waits up to 15 s for the lock to report it: "
                "confirmed true means it is locked, no need to check again."
            ),
            input_schema={"type": "object", "properties": lock_arg},
            handler=lock_door,
            needs_clean_turn=True,
            describe=describe_lock,
        ),
        unlock_tool,
        Tool(
            name="get_lock_activity",
            description=(
                "The latest entries of the Nuki locks' activity log, newest first: when, which "
                "lock, what (unlock, lock, unlatch, door_opened...), by whom (the app user, "
                "keypad code or fob name), how (app, button, keypad, auto_lock...), and whether "
                f"it succeeded. limit: 10 by default, at most {MAX_LOG_ENTRIES}; lock: one "
                "lock's name or id, all locks when left out. After it, no new site opens in "
                "this request."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **lock_arg,
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LOG_ENTRIES},
                },
            },
            handler=get_lock_activity,
        ),
    ]
