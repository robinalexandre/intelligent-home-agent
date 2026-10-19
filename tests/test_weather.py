import httpx
import pytest
import respx

from intelligent_home_agent.config import WindSpot
from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools.weather import (
    FORECAST_URL,
    GEOCODING_URL,
    OpenMeteo,
    build_weather_tools,
    build_wind_tools,
    compass_point,
    direction_matches,
    summarize_spot_day,
)

DAY = "2026-10-14"
SPOT = WindSpot(
    name="North Beach", latitude=0.0, longitude=0.0, directions=["W", "SW"], min_knots=12
)


def hourly(speeds, directions, gusts=None):
    return {
        "time": [f"{DAY}T{h:02d}:00" for h in range(24)],
        "wind_speed_10m": speeds,
        "wind_gusts_10m": gusts or [s + 5 for s in speeds],
        "wind_direction_10m": directions,
    }


@pytest.fixture
async def meteo(monkeypatch):
    async with httpx.AsyncClient() as http:
        client = OpenMeteo(http, "Europe/Paris")
        monkeypatch.setattr(client, "now_hour", lambda: f"{DAY}T00:00")
        yield client


def test_compass_and_sectors():
    assert compass_point(0) == "N"
    assert compass_point(250) == "WSW"
    assert compass_point(355) == "N"
    assert direction_matches(247.5, ["SW"])
    assert direction_matches(290, ["W"])
    assert not direction_matches(90, ["W", "SW"])
    assert direction_matches(90, [])


def test_rideable_windows_need_daylight_strength_and_direction():
    speeds = [20] * 24
    directions = [270] * 24
    for h in (12, 13):
        speeds[h] = 8  # lull
    for h in (16, 17):
        directions[h] = 90  # offshore, wrong direction
    summary = summarize_spot_day(
        SPOT, hourly(speeds, directions), f"{DAY}T08:10", f"{DAY}T19:05", f"{DAY}T10:00"
    )
    assert summary["max_knots"] == 20
    assert summary["direction_at_max"] == "W"
    assert [(w["from"], w["to"]) for w in summary["rideable_windows"]] == [
        ("10:00", "12:00"),
        ("14:00", "16:00"),
        ("18:00", "20:00"),
    ]


def test_no_daylight_left():
    summary = summarize_spot_day(
        SPOT, hourly([20] * 24, [270] * 24), f"{DAY}T08:10", f"{DAY}T19:05", f"{DAY}T21:00"
    )
    assert summary == {"date": DAY, "daylight_hours_left": False}


def test_unknown_spot_direction_fails_at_startup():
    bad = WindSpot(name="X", latitude=0, longitude=0, directions=["OUEST"])
    with pytest.raises(ValueError, match="OUEST"):
        build_wind_tools(None, [bad], model="best_match")


@respx.mock
async def test_wind_tool_queries_all_spots_at_once(meteo, ctx):
    other = WindSpot(name="Lake", latitude=1.0, longitude=1.0)
    location = {
        "hourly": hourly([15] * 24, [225] * 24),
        "daily": {"sunrise": [f"{DAY}T08:10"], "sunset": [f"{DAY}T19:05"]},
    }
    route = respx.get(FORECAST_URL).respond(json=[location, location])
    (tool,) = build_wind_tools(meteo, [SPOT, other], model="meteofrance_seamless")

    result = await tool.handler({"days": 1}, ctx)

    params = route.calls.last.request.url.params
    assert params["latitude"] == "0.0,1.0"
    assert params["wind_speed_unit"] == "kn"
    assert params["models"] == "meteofrance_seamless"
    assert [s["name"] for s in result["spots"]] == ["North Beach", "Lake"]
    window = result["spots"][0]["days"][0]["rideable_windows"][0]
    assert window == {
        "from": "08:00",
        "to": "20:00",
        "avg_knots": 15,
        "max_gust_knots": 20,
        "direction": "SW",
    }


@respx.mock
async def test_weather_for_a_named_place(meteo, ctx):
    respx.get(GEOCODING_URL).respond(
        json={
            "results": [{"name": "Lyon", "country": "France", "latitude": 45.75, "longitude": 4.85}]
        }
    )
    forecast = respx.get(FORECAST_URL).respond(
        json={
            "current": {"temperature_2m": 18.2, "weather_code": 61},
            "hourly": {
                "time": [f"{DAY}T00:00", f"{DAY}T01:00"],
                "temperature_2m": [12, 11],
                "precipitation_probability": [80, 60],
                "weather_code": [63, 3],
            },
            "daily": {
                "time": [DAY],
                "weather_code": [63],
                "temperature_2m_max": [19],
                "temperature_2m_min": [10],
                "precipitation_probability_max": [80],
                "precipitation_sum": [6.5],
                "wind_speed_10m_max": [20],
                "wind_gusts_10m_max": [45],
                "sunrise": [f"{DAY}T08:10"],
                "sunset": [f"{DAY}T19:05"],
            },
        }
    )
    (tool,) = build_weather_tools(meteo, home=None, language="fr")

    result = await tool.handler({"place": "Lyon", "days": 1}, ctx)

    assert forecast.calls.last.request.url.params["latitude"] == "45.75"
    assert result["current"]["conditions"] == "light rain"
    assert result["next_hours"][0] == {
        "time": "00:00",
        "temperature_c": 12,
        "rain_probability_pct": 80,
        "conditions": "rain",
    }
    assert result["days"][0]["sunset"] == "19:05"


async def test_weather_without_home_or_place_asks_for_a_city(meteo, ctx):
    (tool,) = build_weather_tools(meteo, home=None, language="fr")
    with pytest.raises(ToolError, match="city"):
        await tool.handler({}, ctx)
