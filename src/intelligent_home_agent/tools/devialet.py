"""A Devialet Phantom speaker, through its local IP Control API (plain HTTP, no login).

The speaker sits on the home network: from elsewhere the agent reaches it through a VPN
sidecar's HTTP proxy, routed by a subnet router at home (docs/home-devices.md). The speaker needs a
fixed address in the router, as the API has no discovery the agent could use from afar.

Volume is capped (DEVIALET_MAX_VOLUME) so a misheard request can't blast the room.
"""

from __future__ import annotations

from typing import Any

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

API = "/ipcontrol/v1"
PLAYBACK = ("play", "pause", "mute", "unmute", "next", "previous")
# Sources a request can switch to: the speaker's physical inputs. Streaming sources
# (AirPlay, Chromecast, Spotify Connect...) start from the phone that streams.
SWITCHABLE = ("optical", "opticaljack", "line", "phono", "bluetooth", "hdmi")


class Devialet:
    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._base = url.rstrip("/") + API

    async def get(self, path: str) -> dict[str, Any]:
        return await self._send("GET", path)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> None:
        await self._send("POST", path, body if body is not None else {})

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._http.request(method, self._base + path, json=body)
        except httpx.HTTPError as exc:
            raise ToolError(
                "The Devialet speaker is unreachable (off at the wall, or the home network "
                f"can't be reached): {type(exc).__name__}."
            ) from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("details", "") or ""
            except ValueError:
                pass
            raise ToolError(f"The Devialet speaker refused (HTTP {resp.status_code}) {detail}")
        return resp.json() if resp.content else {}


def build_devialet_tools(speaker: Devialet, max_volume: int = 70) -> list[Tool]:
    async def get_speaker_state(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        device = await speaker.get("/devices/current")
        current = await speaker.get("/groups/current/sources/current")
        volume = await speaker.get("/systems/current/sources/current/soundControl/volume")
        sources = await speaker.get("/groups/current/sources")
        return {
            "speaker": device.get("deviceName") or device.get("model"),
            "volume": volume.get("volume"),
            "max_volume": max_volume,
            "playing": current.get("playingState"),
            "muted": current.get("muteState") == "muted",
            "source": (current.get("source") or {}).get("type"),
            "sources": sorted({s["type"] for s in sources.get("sources", [])}),
        }

    async def get_speaker_track(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        metadata = (await speaker.get("/groups/current/sources/current")).get("metadata") or {}
        track = {k: str(metadata[k])[:200] for k in ("title", "artist", "album") if k in metadata}
        return {"now_playing": track or None}

    async def control_speaker(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        done: dict[str, Any] = {}
        if (source := args.get("source")) is not None:
            listed = (await speaker.get("/groups/current/sources")).get("sources", [])
            match = [s for s in listed if s.get("type") == source]
            if not match:
                available = ", ".join(sorted({s["type"] for s in listed} & set(SWITCHABLE)))
                raise ToolError(
                    f"No {source!r} input on this speaker. Inputs: {available or 'none'}."
                )
            await speaker.post(f"/groups/current/sources/{match[0]['sourceId']}/playback/play")
            done["source"] = source
        if (volume := args.get("volume")) is not None:
            level = max(0, min(int(volume), max_volume))
            await speaker.post(
                "/systems/current/sources/current/soundControl/volume", {"volume": level}
            )
            done["volume"] = level
            if level < int(volume):
                done["note"] = f"Capped at {max_volume}, the most the user allows."
        elif (step := args.get("volume_step")) in ("up", "down"):
            await speaker.post(
                f"/systems/current/sources/current/soundControl/volume{step.title()}"
            )
            done["volume_step"] = step
        if (action := args.get("playback")) is not None:
            await speaker.post(f"/groups/current/sources/current/playback/{action}")
            done["playback"] = action
        if args.get("standby"):
            await speaker.post("/systems/current/powerOff")
            done["standby"] = True
        if not done:
            raise ToolError("Say what to change: volume, volume_step, playback, source or standby.")
        return {"status": "done", **done}

    return [
        Tool(
            name="get_speaker_state",
            description=(
                "The Devialet Phantom speaker: volume (0-100), whether it plays, the current "
                "source (airplay2, chromecast, spotify, optical, bluetooth...)."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_speaker_state,
        ),
        Tool(
            name="get_speaker_track",
            description=(
                "The title, artist and album the Devialet speaker plays now. Whoever streams "
                "writes them, so this counts as reading others' text."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_speaker_track,
            taints=True,
        ),
        Tool(
            name="control_speaker",
            description=(
                "Control the Devialet Phantom speaker: set the volume (0-100, capped at "
                f"{max_volume}) or step it up/down, play, pause, mute, unmute, next, "
                "previous, switch to a physical input (optical, bluetooth...), or put it in "
                "standby. Streaming (AirPlay, Spotify) starts from the phone, not here."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100},
                    "volume_step": {"type": "string", "enum": ["up", "down"]},
                    "playback": {"type": "string", "enum": list(PLAYBACK)},
                    "source": {"type": "string", "enum": list(SWITCHABLE)},
                    "standby": {"type": "boolean"},
                },
            },
            handler=control_speaker,
            needs_clean_turn=True,
        ),
    ]
