import json

import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools.hue import Hue, build_hue_tools, rgb_to_xy

BRIDGE = "http://192.168.1.14"
BASE = f"{BRIDGE}/api/key1"

GROUPS = {
    "1": {"name": "Bedroom", "type": "Room", "lights": ["8"], "state": {"any_on": False}},
    "4": {"name": "Lounge", "type": "Room", "lights": ["5", "6"], "state": {"any_on": True}},
    "9": {"name": "Entertainment", "type": "Entertainment", "lights": ["5"], "state": {}},
}
LIGHTS = {
    "5": {"name": "Lounge 2", "state": {"on": True, "bri": 127, "reachable": True, "xy": [0, 0]}},
    "6": {"name": "Lounge", "state": {"on": True, "bri": 254, "reachable": True, "xy": [0, 0]}},
    "8": {"name": "Bedroom", "state": {"on": False, "bri": 254, "reachable": False}},
}
SCENES = {
    "s1": {"name": "Relax", "group": "4"},
    "s2": {"name": "Reading", "group": "4"},
    "s3": {"name": "Nightlight", "group": "1"},
}


def tools(http):
    return {t.name: t for t in build_hue_tools(Hue(http, BRIDGE, "key1"))}


def mock_bridge():
    respx.get(f"{BASE}/groups").mock(return_value=httpx.Response(200, json=GROUPS))
    respx.get(f"{BASE}/lights").mock(return_value=httpx.Response(200, json=LIGHTS))
    respx.get(f"{BASE}/scenes").mock(return_value=httpx.Response(200, json=SCENES))


def sent(route):
    return json.loads(route.calls[0].request.content)


@respx.mock
async def test_rooms_list_their_lights_and_scenes(ctx):
    mock_bridge()
    async with httpx.AsyncClient() as http:
        result = await tools(http)["list_hue_lights"].handler({}, ctx)

    rooms = {r["room"]: r for r in result["rooms"]}
    assert set(rooms) == {"Bedroom", "Lounge"}
    assert rooms["Lounge"]["scenes"] == ["Reading", "Relax"]
    assert rooms["Lounge"]["lights"][0] == {
        "name": "Lounge 2",
        "on": True,
        "brightness_pct": 50,
        "reachable": True,
        "color": True,
    }
    assert rooms["Bedroom"]["lights"][0]["reachable"] is False


@respx.mock
async def test_a_name_shared_by_a_room_and_a_light_targets_the_room(ctx):
    mock_bridge()
    action = respx.put(f"{BASE}/groups/4/action").mock(
        return_value=httpx.Response(200, json=[{"success": {}}])
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["set_hue_lights"].handler(
            {"target": "lounge", "brightness_pct": 30}, ctx
        )

    assert result["room"] == "Lounge"
    assert sent(action) == {"on": True, "bri": 76}


@respx.mock
async def test_a_light_alone_gets_its_own_state_and_a_named_colour(ctx):
    mock_bridge()
    state = respx.put(f"{BASE}/lights/5/state").mock(
        return_value=httpx.Response(200, json=[{"success": {}}])
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["set_hue_lights"].handler(
            {"target": "Lounge 2", "color": "Blue"}, ctx
        )

    assert result["light"] == "Lounge 2"
    assert sent(state) == {"xy": rgb_to_xy("#0000ff"), "on": True}


@respx.mock
async def test_all_targets_every_light_and_off_wins(ctx):
    mock_bridge()
    action = respx.put(f"{BASE}/groups/0/action").mock(
        return_value=httpx.Response(200, json=[{"success": {}}])
    )
    async with httpx.AsyncClient() as http:
        await tools(http)["set_hue_lights"].handler(
            {"target": "tout", "on": False, "color_temp_kelvin": 2700}, ctx
        )

    assert sent(action) == {"on": False}


@respx.mock
async def test_white_temperature_becomes_mireds(ctx):
    mock_bridge()
    action = respx.put(f"{BASE}/groups/1/action").mock(
        return_value=httpx.Response(200, json=[{"success": {}}])
    )
    async with httpx.AsyncClient() as http:
        await tools(http)["set_hue_lights"].handler(
            {"target": "bedroom", "color_temp_kelvin": 2700}, ctx
        )

    assert sent(action) == {"ct": 370, "on": True}


@respx.mock
async def test_a_scene_is_found_by_name_within_its_room(ctx):
    mock_bridge()
    action = respx.put(f"{BASE}/groups/4/action").mock(
        return_value=httpx.Response(200, json=[{"success": {}}])
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["activate_hue_scene"].handler(
            {"room": "Lounge", "scene": "relax"}, ctx
        )
        with pytest.raises(ToolError, match="Reading, Relax"):
            await tools(http)["activate_hue_scene"].handler(
                {"room": "Lounge", "scene": "Nightlight"}, ctx
            )

    assert result == {"status": "done", "room": "Lounge", "scene": "Relax"}
    assert sent(action) == {"scene": "s1"}


@respx.mock
async def test_unknown_room_lists_the_real_ones(ctx):
    mock_bridge()
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="Rooms: Bedroom, Lounge"):
            await tools(http)["set_hue_lights"].handler({"target": "kitchen", "on": True}, ctx)


@respx.mock
async def test_a_refused_key_says_to_pair_again(ctx):
    respx.get(f"{BASE}/groups").mock(
        return_value=httpx.Response(
            200, json=[{"error": {"type": 1, "description": "unauthorized user"}}]
        )
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="pair it again"):
            await tools(http)["list_hue_lights"].handler({}, ctx)


@respx.mock
async def test_an_unreachable_bridge_is_explained(ctx):
    respx.get(f"{BASE}/groups").mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="unreachable"):
            await tools(http)["list_hue_lights"].handler({}, ctx)


async def test_nothing_to_change_is_refused(ctx):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="Say what to change"):
            await tools(http)["set_hue_lights"].handler({"target": "Lounge"}, ctx)


def test_writes_need_a_clean_request():
    tool_map = {t.name: t for t in build_hue_tools(Hue(None, BRIDGE, "k"))}
    assert tool_map["set_hue_lights"].needs_clean_turn
    assert tool_map["activate_hue_scene"].needs_clean_turn
    assert not tool_map["list_hue_lights"].needs_clean_turn
