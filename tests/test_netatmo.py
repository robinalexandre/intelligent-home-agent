import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError, ToolRegistry
from intelligent_home_agent.tools.netatmo import (
    TOKEN_URL,
    Netatmo,
    NetatmoAuth,
    build_netatmo_tools,
)

TZ = "Europe/Paris"
SEEN = int(datetime(2026, 11, 13, 7, 30, tzinfo=UTC).timestamp())

HOMES = {
    "status": "ok",
    "body": {
        "homes": [
            {
                "id": "h1",
                "name": "Home",
                "rooms": [{"id": "1001", "name": "Living room"}],
                "modules": [
                    {"id": "70:ee:50:00:00:01", "type": "NACamera", "name": "Front door"},
                    {"id": "70:ee:50:00:00:02", "type": "NACamera", "name": "Garage"},
                    {"id": "70:ee:50:00:00:03", "type": "NACamera", "name": "Office"},
                    {"id": "70:ee:50:00:00:09", "type": "NAMain", "name": "Station"},
                    {"id": "09:00:00:00:00:01", "type": "NRV", "name": "Valve"},
                ],
                "persons": [
                    {"id": "p1", "pseudo": "Sam", "url": "https://netatmocameraimage/x"},
                    {"id": "p2", "pseudo": "Jo"},
                    {"id": "p3"},
                ],
            },
            {"id": "h2", "name": "Cabin", "modules": []},
        ]
    },
}
STATUS = {
    "status": "ok",
    "body": {
        "home": {
            "id": "h1",
            "modules": [
                {"id": "70:ee:50:00:00:01", "type": "NACamera", "monitoring": "on"},
                {"id": "70:ee:50:00:00:02", "type": "NACamera", "monitoring": "off"},
                {
                    "id": "70:ee:50:00:00:03",
                    "type": "NACamera",
                    "monitoring": "on",
                    "vpn_url": "https://prodvpn-eu-5.netatmo.net/secret",
                },
            ],
            "persons": [
                {"id": "p1", "last_seen": SEEN, "out_of_sight": False},
                {"id": "p2", "last_seen": str(SEEN - 3600), "out_of_sight": True},
                {"id": "p3", "last_seen": SEEN, "out_of_sight": False},
            ],
            "rooms": [
                {
                    "id": "1001",
                    "reachable": True,
                    "therm_measured_temperature": 19.5,
                    "therm_setpoint_temperature": 20,
                    "therm_setpoint_mode": "schedule",
                    "heating_power_request": 40,
                    "open_windows": False,
                }
            ],
        },
        "errors": [{"id": "70:ee:50:00:00:03", "code": 6}],
    },
}
SNAPSHOT = {"url": "https://netatmocameraimage.blob.core.windows.net/production/abc"}
EVENTS = {
    "status": "ok",
    "body": {
        "home": {
            "id": "h1",
            "events": [
                {
                    "id": "e3",
                    "type": "person",
                    "time": SEEN,
                    "camera_id": "70:ee:50:00:00:01",
                    "person_id": "p1",
                    "is_arrival": True,
                    "snapshot": SNAPSHOT,
                    "message": "<b>Sam</b> seen",
                },
                {
                    "id": "e2",
                    "type": "person",
                    "time": SEEN - 60,
                    "camera_id": "70:ee:50:00:00:01",
                    "person_id": "p3",
                    "snapshot": SNAPSHOT,
                    "video_id": "v1",
                    "message": "<b>Unknown face</b> seen",
                },
                {
                    "id": "e1",
                    "type": "movement",
                    "time": SEEN - 120,
                    "camera_id": "70:ee:50:00:00:02",
                    "vignette": SNAPSHOT,
                    "message": "<b>Motion</b> detected",
                },
                {"id": "e0", "type": "off", "time": SEEN - 600, "camera_id": "70:ee:50:00:00:02"},
            ],
        }
    },
}
STATIONS = {
    "status": "ok",
    "body": {
        "devices": [
            {
                "_id": "70:ee:50:00:00:09",
                "type": "NAMain",
                "home_name": "Home",
                "module_name": "Indoor",
                "reachable": True,
                "dashboard_data": {"time_utc": SEEN, "Temperature": 21.3, "CO2": 640},
                "modules": [
                    {
                        "_id": "02:00:00:00:00:01",
                        "type": "NAModule1",
                        "module_name": "Garden",
                        "reachable": True,
                        "battery_percent": 80,
                        "dashboard_data": {"time_utc": SEEN, "Temperature": 8.1, "Humidity": 90},
                    }
                ],
            }
        ]
    },
}


def token_response(refresh="r2", status=200):
    if status != 200:
        return httpx.Response(status, json={"error": "invalid_grant"})
    return httpx.Response(
        200, json={"access_token": "a1", "expires_in": 10800, "refresh_token": refresh}
    )


def api(path):
    return respx.route(host="api.netatmo.com", path=f"/api/{path}")


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def tools(http, tmp_path, refresh="r1"):
    auth = NetatmoAuth(http, "c1", "s1", refresh, tmp_path / "netatmo.json")
    return {t.name: t for t in build_netatmo_tools(Netatmo(http, auth), TZ)}


@respx.mock
async def test_security_status_gives_cameras_and_who_is_home(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    homes = api("homesdata").respond(json=HOMES)
    status = api("homestatus").respond(json=STATUS)
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["get_home_security_status"].handler({}, ctx)

    [home] = result["homes"]
    assert home["home"] == "Home"
    assert [(c["name"], c["state"]) for c in home["cameras"]] == [
        ("Front door", "on"),
        ("Garage", "off"),
        ("Office", "disconnected"),
    ]
    assert home["cameras"][0]["id"] == "70:ee:50:00:00:01"
    assert home["persons"] == [
        {"name": "Sam", "home": True, "last_seen": "2026-11-13T08:30+01:00"},
        {"name": "Jo", "home": False, "last_seen": "2026-11-13T07:30+01:00"},
    ]
    assert "http" not in json.dumps(result)
    assert ctx.account_read
    assert homes.calls[0].request.headers["authorization"] == "Bearer a1"
    assert status.calls[0].request.url.params["home_id"] == "h1"


@respx.mock
async def test_events_are_newest_first_with_names_and_no_image_links(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    api("homesdata").respond(json=HOMES)
    events = api("getevents").respond(json=EVENTS)
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["get_camera_events"].handler({"count": 3}, ctx)

    assert result["events"] == [
        {
            "at": "2026-11-13T08:30+01:00",
            "type": "person seen",
            "camera": "Front door",
            "person": "Sam",
            "arrival": True,
            "message": "Sam seen",
        },
        {
            "at": "2026-11-13T08:29+01:00",
            "type": "person seen",
            "camera": "Front door",
            "person": "unknown face",
            "message": "Unknown face seen",
        },
        {
            "at": "2026-11-13T08:28+01:00",
            "type": "movement",
            "camera": "Garage",
            "message": "Motion detected",
        },
    ]
    assert "http" not in json.dumps(result)
    assert ctx.account_read
    assert events.call_count == 1
    assert events.calls[0].request.url.params["home_id"] == "h1"


@respx.mock
async def test_measures_read_the_station_and_the_rooms(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    api("homesdata").respond(json=HOMES)
    api("homestatus").respond(json=STATUS)
    api("getstationsdata").respond(json=STATIONS)
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["get_netatmo_measures"].handler({}, ctx)

    [station] = result["weather_stations"]
    assert station["modules"] == [
        {
            "name": "Indoor",
            "temperature_c": 21.3,
            "co2_ppm": 640,
            "at": "2026-11-13T08:30+01:00",
        },
        {
            "name": "Garden",
            "temperature_c": 8.1,
            "humidity_percent": 90,
            "at": "2026-11-13T08:30+01:00",
            "battery_percent": 80,
        },
    ]
    [room] = result["thermostat_rooms"]
    assert room["room"] == "Living room"
    assert room["temperature_c"] == 19.5
    assert room["setpoint_c"] == 20
    assert ctx.account_read


@respx.mock
async def test_monitoring_waits_for_the_go_then_sets_the_state(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    api("homesdata").respond(json=HOMES)
    setstate = api("setstate").respond(json={"status": "ok", "time_server": SEEN})
    async with httpx.AsyncClient() as http:
        tool = tools(http, tmp_path)["set_camera_monitoring"]
        assert tool.needs_clean_turn and tool.always_propose
        args = {"camera": "70:ee:50:00:00:02", "state": "off"}

        parked = await ToolRegistry([tool]).run(tool.name, args, ctx)
        assert parked["status"] == "waiting_for_the_user"
        assert setstate.call_count == 0
        summary = ctx.actions[0]["summary"]
        assert summary.startswith('Netatmo: turn off monitoring of the camera "Garage"')

        result = await ToolRegistry([tool]).run(tool.name, args, ctx, approved=True)

    assert result == {"camera": "Garage", "home": "Home", "monitoring": "off"}
    assert json.loads(setstate.calls[0].request.content) == {
        "home": {"id": "h1", "modules": [{"id": "70:ee:50:00:00:02", "monitoring": "off"}]}
    }


async def test_monitoring_refuses_a_state_other_than_on_or_off(tmp_path, ctx):
    async with httpx.AsyncClient() as http:
        tool = tools(http, tmp_path)["set_camera_monitoring"]
        with pytest.raises(ToolError, match='"on" or "off"'):
            await ToolRegistry([tool]).run(tool.name, {"state": "maybe"}, ctx)


@respx.mock
async def test_monitoring_asks_which_camera_when_there_are_several(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    api("homesdata").respond(json=HOMES)
    async with httpx.AsyncClient() as http:
        tool = tools(http, tmp_path)["set_camera_monitoring"]
        with pytest.raises(ToolError, match="Which camera"):
            await tool.handler({"state": "on"}, ctx)


@respx.mock
async def test_an_unreachable_camera_is_reported(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    api("homesdata").respond(json=HOMES)
    errors = {"status": "ok", "body": {"errors": [{"id": "70:ee:50:00:00:03", "code": 6}]}}
    api("setstate").respond(json=errors)
    async with httpx.AsyncClient() as http:
        tool = tools(http, tmp_path)["set_camera_monitoring"]
        with pytest.raises(ToolError, match="Office is unreachable"):
            await tool.handler({"camera": "70:ee:50:00:00:03", "state": "on"}, ctx)


@respx.mock
async def test_rotated_refresh_token_is_saved_and_reused(tmp_path):
    store = tmp_path / "netatmo.json"
    route = respx.post(TOKEN_URL).mock(return_value=token_response("r2"))
    async with httpx.AsyncClient() as http:
        assert await NetatmoAuth(http, "c1", "s1", "r1", store).access_token() == "a1"
        assert json.loads(store.read_text()) == {"client_id": "c1", "refresh_token": "r2"}
        await NetatmoAuth(http, "c1", "s1", "r1", store).access_token()

    first = form(route.calls[0].request)
    assert first == {
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "client_id": "c1",
        "client_secret": "s1",
    }
    assert form(route.calls[1].request)["refresh_token"] == "r2"
    assert oct(store.stat().st_mode)[-3:] == "600"


@respx.mock
async def test_a_new_login_replaces_a_dead_saved_token(tmp_path):
    store = tmp_path / "netatmo.json"
    store.write_text(json.dumps({"client_id": "c1", "refresh_token": "old"}))
    route = respx.post(TOKEN_URL).mock(side_effect=[token_response(status=400), token_response()])
    async with httpx.AsyncClient() as http:
        assert await NetatmoAuth(http, "c1", "s1", "new", store).access_token() == "a1"

    assert [form(c.request)["refresh_token"] for c in route.calls] == ["old", "new"]
    assert json.loads(store.read_text())["refresh_token"] == "r2"


@respx.mock
async def test_concurrent_calls_refresh_once(tmp_path):
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    async with httpx.AsyncClient() as http:
        auth = NetatmoAuth(http, "c1", "s1", "r1", tmp_path / "n.json")
        await asyncio.gather(auth.access_token(), auth.access_token())

    assert route.call_count == 1


@respx.mock
async def test_an_expired_token_is_renewed_once(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(side_effect=[token_response("r2"), token_response("r3")])
    expired = {"error": {"code": 3, "message": "Access token expired"}}
    respx.route(host="api.netatmo.com", path="/api/homesdata").mock(
        side_effect=[httpx.Response(403, json=expired), httpx.Response(200, json=HOMES)]
    )
    api("homestatus").respond(json=STATUS)
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["get_home_security_status"].handler({}, ctx)

    assert len(result["homes"][0]["cameras"]) == 3


@respx.mock
async def test_a_refused_refresh_says_to_log_in_again(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response(status=400))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="netatmo_login.py again"):
            await tools(http, tmp_path)["get_camera_events"].handler({}, ctx)


@respx.mock
async def test_the_rate_limit_asks_to_wait(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    limited = {"error": {"code": 26, "message": "User usage reached"}}
    api("homesdata").respond(status_code=429, json=limited)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="try again later"):
            await tools(http, tmp_path)["get_home_security_status"].handler({}, ctx)


async def test_without_login_it_says_how_to_connect(tmp_path):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="netatmo_login"):
            await NetatmoAuth(http, "c1", "s1", None, tmp_path / "n.json").access_token()
