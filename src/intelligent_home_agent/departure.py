"""Leaving home: what is still unlocked, on or playing, and an offer to fix it.

The agent learns that the user left when a client calls ``POST /v1/departure`` (the
phone's "When I leave home" automation, a Home Assistant automation, a geofence app).

The code reads the devices through the agent's own tools, whichever are set up, and writes
every word of the message itself; no model is involved. Directly driven devices come
first (a Nuki lock, Hue lights, the speaker, the Android TV device, Apple TV and HomePods);
where none covers a kind of device, Home Assistant's entities are read instead (locks,
lights, media players). When all is shut, nothing is said. Otherwise each fix is parked
as a write waiting for the user's go, the one mechanism there is (tools/proposals.py): it
is listed, numbered, under the message, and runs only on their yes in a later message of
the same conversation. Nothing is locked or turned off without it.

A lock whose door is open is said but not offered: the bolt would close on nothing. A
Home Assistant lock is said but not offered either: locks are a sensitive domain there,
confirmed by the user in their own words.

Seam: the private setup this comes from also noticed a departure by itself, from the
phone leaving the home Wi-Fi (read on the router). That needs a router or presence tool,
which is not ported.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

LOCKED_STATES = {"locked", "locking"}
MAX_NAMES = 5
# What the conversation's history says happened, for the model that reads the user's yes.
NOTE = "[The user left home; the code checked the home's devices and told them this.]"


@dataclass
class Finding:
    line: str
    # The fix as the user reads it, and the tool call it runs; empty: nothing to offer.
    fix: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    # Said even when nothing can be fixed: an open door, a lock that could not be read.
    urgent: bool = False


@dataclass
class Report:
    # The message for the user, without the list of what waits; "" when all is shut.
    text: str = ""
    # {"type": "proposal", ...} for each fix parked, for main.with_waiting.
    actions: list[dict[str, Any]] = field(default_factory=list)


class Departure:
    def __init__(self, tools: ToolRegistry, *, language: str = "en", tv_name: str = "TV") -> None:
        """tv_name: what the user calls the Android TV device ("TV", "projector")."""
        self._tools = tools
        self._s = strings(language)
        self._tv = tv_name

    def _has(self, name: str) -> bool:
        return self._tools.get(name) is not None

    @property
    def watches_something(self) -> bool:
        return any(self._has(name) for name in READ_TOOLS)

    async def _run(self, name: str, args: dict[str, Any]) -> Any:
        # A conversation of its own: what the reads return never enters the user's.
        return await self._tools.run(name, args, ToolContext(Conversation(id="departure")))

    async def check(self, conv: Conversation) -> Report:
        """Read the devices; say what is left and park each fix in the conversation."""
        s = self._s
        findings = await self.findings()
        if not any(f.fix or f.urgent for f in findings):
            return Report()
        text = "\n".join([s.departure_header, *(f.line for f in findings)])
        fixes = [f for f in findings if f.fix]
        if not fixes:
            text += "\n" + s.departure_no_fix
        conv.turn += 1
        actions = []
        for finding in fixes:
            proposal, new = conv.propose(finding.tool, finding.args, finding.fix)
            if new or not proposal.shown:
                actions.append(
                    {"type": "proposal", "number": proposal.number, "summary": finding.fix}
                )
        return Report(text, actions)

    async def findings(self) -> list[Finding]:
        s = self._s
        readers: list[tuple[Callable[[], Awaitable[list[Finding]]], str, bool]] = [
            (self._lock, s.departure_lock, True),
            (self._lights, s.departure_lights, False),
            (self._speaker, s.departure_speaker, False),
            (self._tv_device, s.departure_tv, False),
            (self._apple, s.departure_apple, False),
            (self._home_assistant, s.departure_ha, False),
        ]
        found: list[Finding] = []
        for read, what, urgent in readers:
            try:
                found += await read()
            except ToolError as exc:
                log.warning("Departure check: %s", exc)
                found.append(Finding(s.departure_unreadable.format(what=what), urgent=urgent))
        return found

    def _names(self, names: list[str]) -> str:
        shown = ", ".join(names[:MAX_NAMES])
        if len(names) > MAX_NAMES:
            shown += self._s.departure_and_more.format(count=len(names) - MAX_NAMES)
        return shown

    async def _lock(self) -> list[Finding]:
        if not self._has("get_lock_state"):
            return []
        s = self._s
        locks = (await self._run("get_lock_state", {})).get("locks", [])
        found = []
        for lock in locks:
            if lock.get("device") not in {"smart_lock", "smartdoor"}:
                continue
            name = str(lock.get("name") or s.departure_lock)
            if lock.get("state") in LOCKED_STATES:
                continue
            if lock.get("door") == "open":
                found.append(Finding(s.departure_door_open.format(name=name), urgent=True))
                continue
            found.append(
                Finding(
                    s.departure_not_locked.format(name=name),
                    s.departure_lock_fix.format(name=name),
                    "lock_door",
                    {"lock": name},
                )
            )
        return found

    async def _lights(self) -> list[Finding]:
        if not self._has("list_hue_lights"):
            return []
        on: dict[str, None] = {}
        for room in (await self._run("list_hue_lights", {})).get("rooms", []):
            for light in room.get("lights", []):
                if light.get("on") and light.get("reachable", True):
                    on[str(light.get("name"))] = None
        if not on:
            return []
        s = self._s
        line = s.departure_lights_on.format(count=len(on), names=self._names(list(on)))
        fix = {"target": "all", "on": False}
        return [Finding(line, s.departure_lights_fix, "set_hue_lights", fix)]

    async def _speaker(self) -> list[Finding]:
        if not self._has("get_speaker_state"):
            return []
        state = await self._run("get_speaker_state", {})
        if state.get("playing") != "playing" or state.get("muted"):
            return []
        s = self._s
        return [
            Finding(
                s.departure_speaker_playing,
                s.departure_speaker_fix,
                "control_speaker",
                {"standby": True},
            )
        ]

    async def _tv_device(self) -> list[Finding]:
        if not self._has("get_tv_state"):
            return []
        state = await self._run("get_tv_state", {})
        if not state.get("on"):
            return []
        s = self._s
        app = f" ({state['app']})" if state.get("app") else ""
        return [
            Finding(
                s.departure_tv_on.format(name=self._tv, app=app),
                s.departure_tv_fix.format(name=self._tv),
                "control_tv",
                {"power": "off"},
            )
        ]

    async def _apple(self) -> list[Finding]:
        if not self._has("get_apple_media_state"):
            return []
        s = self._s
        found = []
        for row in (await self._run("get_apple_media_state", {})).get("devices", []):
            name = str(row.get("device"))
            # Asleep off the network is off; a HomePod that speaks for the Apple TV shows
            # the TV's state with its own power off, and the TV is said already.
            if row.get("reachable") is False or not row.get("on"):
                continue
            if row.get("kind") == "apple_tv":
                app = f" ({row['app']})" if row.get("app") else ""
                found.append(
                    Finding(
                        s.departure_apple_tv_on.format(name=name, app=app),
                        s.departure_apple_tv_fix.format(name=name),
                        "control_apple_media",
                        {"device": name, "action": "turn_off"},
                    )
                )
            elif row.get("playing"):
                found.append(
                    Finding(
                        s.departure_homepod_playing.format(name=name),
                        s.departure_homepod_fix.format(name=name),
                        "control_apple_media",
                        {"device": name, "action": "pause"},
                    )
                )
        return found

    async def _home_assistant(self) -> list[Finding]:
        """Home Assistant's entities, for each kind of device no direct tool covers."""
        if not (self._has("list_entities") and self._has("call_service")):
            return []
        s = self._s
        found: list[Finding] = []
        if not self._has("get_lock_state"):
            for lock in await self._entities("lock"):
                if lock.get("state") == "unlocked":
                    line = s.departure_not_locked_ask.format(name=lock["name"])
                    found.append(Finding(line, urgent=True))
        if not self._has("list_hue_lights"):
            on = [e for e in await self._entities("light") if e.get("state") == "on"]
            if on:
                names = self._names([str(e["name"]) for e in on])
                found.append(
                    Finding(
                        s.departure_lights_on.format(count=len(on), names=names),
                        s.departure_lights_fix,
                        "call_service",
                        {
                            "domain": "light",
                            "service": "turn_off",
                            "entity_id": [e["entity_id"] for e in on],
                        },
                    )
                )
        if not any(self._has(name) for name in MEDIA_READ_TOOLS):
            for player in await self._entities("media_player"):
                if player.get("state") != "playing":
                    continue
                found.append(
                    Finding(
                        s.departure_player_playing.format(name=player["name"]),
                        s.departure_player_fix.format(name=player["name"]),
                        "call_service",
                        {
                            "domain": "media_player",
                            "service": "media_pause",
                            "entity_id": [player["entity_id"]],
                        },
                    )
                )
        return found

    async def _entities(self, domain: str) -> list[dict[str, Any]]:
        return (await self._run("list_entities", {"domain": domain})).get("entities", [])


MEDIA_READ_TOOLS = ("get_speaker_state", "get_tv_state", "get_apple_media_state")
READ_TOOLS = ("get_lock_state", "list_hue_lights", *MEDIA_READ_TOOLS, "list_entities")


def record(conv: Conversation, text: str) -> None:
    """Keep the check in the conversation's history, so the model that reads the user's
    answer knows what "yes" is about. Every word is the code's own."""
    conv.messages.append({"role": "user", "content": NOTE})
    conv.messages.append({"role": "assistant", "content": text})
