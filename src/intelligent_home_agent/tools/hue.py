"""Philips Hue lights, through the bridge's local API (v1, plain HTTP).

The bridge sits on the home network: from elsewhere the agent reaches it through a VPN
sidecar's HTTP proxy, routed by a subnet router at home (docs/home-devices.md). The app key
comes from pressing the bridge's link button once.

Rooms, lights and scenes are found by name, accents and case aside, so the model passes
what the user said ("lounge", "the bedroom") and the code picks the target: it never sends
a raw id the bridge did not list.
"""

from __future__ import annotations

import unicodedata
from typing import Any

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

ALL_LIGHTS = "0"
_ALL_NAMES = {"all", "tout", "toutes", "maison", "partout", "home", "everywhere"}
MIN_KELVIN = 2000
MAX_KELVIN = 6500
COLORS = {
    "red": "#ff0000",
    "orange": "#ff8000",
    "yellow": "#ffd000",
    "green": "#00ff00",
    "cyan": "#00ffff",
    "blue": "#0000ff",
    "purple": "#8000ff",
    "pink": "#ff40a0",
    "white": "#ffffff",
}


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def _pct(bri: int | None) -> int | None:
    return None if bri is None else round(bri * 100 / 254)


def _bri(pct: float) -> int:
    return max(1, min(254, round(pct * 254 / 100)))


def rgb_to_xy(hex_color: str) -> list[float]:
    """CIE xy of an sRGB colour, as Philips' own conversion does (wide gamut, D65)."""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ToolError(f"Colour {hex_color!r} is not #RRGGBB.")
    try:
        rgb = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError as exc:
        raise ToolError(f"Colour {hex_color!r} is not #RRGGBB.") from exc
    r, g, b = (((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in rgb)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    if total == 0:
        return [0.3127, 0.3290]
    return [round(x / total, 4), round(y / total, 4)]


class Hue:
    def __init__(self, http: httpx.AsyncClient, bridge_url: str, app_key: str) -> None:
        self._http = http
        self._base = f"{bridge_url.rstrip('/')}/api/{app_key}"

    async def get(self, path: str) -> dict[str, Any]:
        return await self._send("GET", path)

    async def put(self, path: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._send("PUT", path, body)

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._http.request(method, f"{self._base}/{path}", json=body)
        except httpx.HTTPError as exc:
            raise ToolError(
                "The Hue bridge is unreachable (the home network can't be reached): "
                f"{type(exc).__name__}."
            ) from exc
        if resp.status_code != 200:
            raise ToolError(f"The Hue bridge answered HTTP {resp.status_code}.")
        data = resp.json()
        errors = (
            [item["error"] for item in data if "error" in item] if isinstance(data, list) else []
        )
        if errors:
            if any(e.get("type") == 1 for e in errors):
                raise ToolError("The Hue bridge refused the app key: pair it again.")
            raise ToolError("Hue: " + "; ".join(e.get("description", "?") for e in errors))
        return data


def build_hue_tools(hue: Hue) -> list[Tool]:
    async def snapshot() -> tuple[dict, dict, dict]:
        return await hue.get("groups"), await hue.get("lights"), await hue.get("scenes")

    def find_room(groups: dict[str, Any], name: str) -> tuple[str, str]:
        wanted = _normalize(name)
        if wanted in _ALL_NAMES:
            return ALL_LIGHTS, "the whole home"
        rooms = {k: g for k, g in groups.items() if g.get("type") in ("Room", "Zone")}
        for key, group in rooms.items():
            if _normalize(group["name"]) == wanted:
                return key, group["name"]
        partial = [(k, g["name"]) for k, g in rooms.items() if wanted in _normalize(g["name"])]
        if len(partial) == 1:
            return partial[0]
        raise ToolError(
            f"No Hue room named {name!r}. Rooms: {', '.join(g['name'] for g in rooms.values())}."
        )

    def find_light(lights: dict[str, Any], name: str) -> tuple[str, str] | None:
        wanted = _normalize(name)
        for key, light in lights.items():
            if _normalize(light["name"]) == wanted:
                return key, light["name"]
        return None

    async def list_lights(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        groups, lights, scenes = await snapshot()
        rooms = []
        for key, group in groups.items():
            if group.get("type") not in ("Room", "Zone"):
                continue
            members = [lights[i] for i in group.get("lights", []) if i in lights]
            rooms.append(
                {
                    "room": group["name"],
                    "any_on": group.get("state", {}).get("any_on", False),
                    "lights": [
                        {
                            "name": light["name"],
                            "on": light["state"].get("on"),
                            "brightness_pct": _pct(light["state"].get("bri")),
                            "reachable": light["state"].get("reachable", True),
                            "color": "xy" in light["state"],
                        }
                        for light in members
                    ],
                    "scenes": sorted({s["name"] for s in scenes.values() if s.get("group") == key}),
                }
            )
        return {"rooms": rooms}

    def state_from(args: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {}
        if args.get("on") is not None:
            state["on"] = bool(args["on"])
        if args.get("brightness_pct") is not None:
            pct = float(args["brightness_pct"])
            if pct <= 0:
                state["on"] = False
            else:
                state["on"] = True
                state["bri"] = _bri(pct)
        if args.get("color_temp_kelvin") is not None:
            kelvin = min(max(int(args["color_temp_kelvin"]), MIN_KELVIN), MAX_KELVIN)
            state["ct"] = max(153, min(500, round(1_000_000 / kelvin)))
            state.setdefault("on", True)
        if color := args.get("color"):
            hex_color = COLORS.get(_normalize(color), color)
            state["xy"] = rgb_to_xy(hex_color)
            state.setdefault("on", True)
        if not state:
            raise ToolError("Say what to change: on, brightness_pct, color_temp_kelvin or color.")
        if state.get("on") is False:
            return {"on": False}
        return state

    async def set_lights(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        state = state_from(args)
        groups, lights, _scenes = await snapshot()
        target = args["target"]
        light = find_light(lights, target)
        if light and not any(_normalize(g["name"]) == _normalize(target) for g in groups.values()):
            key, name = light
            await hue.put(f"lights/{key}/state", state)
            return {"status": "done", "light": name, "state": state}
        key, name = find_room(groups, target)
        await hue.put(f"groups/{key}/action", state)
        return {"status": "done", "room": name, "state": state}

    async def activate_scene(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        groups, _lights, scenes = await snapshot()
        key, room = find_room(groups, args["room"])
        wanted = _normalize(args["scene"])
        in_room = {k: s for k, s in scenes.items() if s.get("group") == key}
        found = [k for k, s in in_room.items() if _normalize(s["name"]) == wanted]
        if not found:
            names = sorted({s["name"] for s in in_room.values()})
            raise ToolError(
                f"No scene {args['scene']!r} in {room}. Scenes there: {', '.join(names) or 'none'}."
            )
        await hue.put(f"groups/{key}/action", {"scene": found[0]})
        return {"status": "done", "room": room, "scene": in_room[found[0]]["name"]}

    target_schema = {
        "type": "string",
        "description": "A room or a light, by its Hue name as the user said it; 'all' for "
        "the whole home.",
    }
    return [
        Tool(
            name="list_hue_lights",
            description=(
                "The Philips Hue lights by room: which are on, their brightness, whether they "
                "do colour, and each room's scenes. Use it to find the names to control."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=list_lights,
        ),
        Tool(
            name="set_hue_lights",
            description=(
                "Turn Philips Hue lights on or off, or set their brightness (0-100, 0 turns "
                "off), white temperature (2000 warm to 6500 K cold) or colour (red, orange, "
                "yellow, green, cyan, blue, purple, pink, white, or #RRGGBB) for a room, one "
                "light, or 'all'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "target": target_schema,
                    "on": {"type": "boolean"},
                    "brightness_pct": {"type": "number", "minimum": 0, "maximum": 100},
                    "color_temp_kelvin": {
                        "type": "integer",
                        "minimum": MIN_KELVIN,
                        "maximum": MAX_KELVIN,
                    },
                    "color": {"type": "string"},
                },
                "required": ["target"],
            },
            handler=set_lights,
            needs_clean_turn=True,
        ),
        Tool(
            name="activate_hue_scene",
            description="Recall one of a room's Philips Hue scenes, by its name.",
            input_schema={
                "type": "object",
                "properties": {"room": target_schema, "scene": {"type": "string"}},
                "required": ["room", "scene"],
            },
            handler=activate_scene,
            needs_clean_turn=True,
        ),
    ]
