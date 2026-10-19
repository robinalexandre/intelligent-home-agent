import httpx
import pytest
import respx

from intelligent_home_agent.config import SurfSpot
from intelligent_home_agent.tools.surf import (
    build_surf_tools,
    summarize_surf_day,
    tide_turns,
    wind_effect,
)
from intelligent_home_agent.tools.weather import FORECAST_URL, MARINE_URL, OpenMeteo

DAY = "2026-10-14"
TIMES = [f"{DAY}T{h:02d}:00" for h in range(24)]
SPOT = SurfSpot(name="North Beach", latitude=0.0, longitude=0.0, facing="W")


def marine(heights, periods=None):
    return {
        "time": TIMES,
        "wave_height": heights,
        "wave_period": periods or [10] * 24,
        "wave_direction": [270] * 24,
        "sea_level_height_msl": [-2.3, -2.4, -1.8, -0.9, 0.1, 0.8, 1.06, 1.05, 0.8, 0.3]
        + [-0.5, -1.4, -2.1, -2.4, -2.0, -1.2, -0.2, 0.5, 0.9, 0.94, 0.75, 0.36, -0.24, -1.0],
    }


def wind(knots, directions):
    return {"time": TIMES, "wind_speed_10m": knots, "wind_direction_10m": directions}


def test_wind_effect_relative_to_the_beach():
    assert wind_effect("W", 90, 15) == "offshore"
    assert wind_effect("W", 260, 15) == "onshore"
    assert wind_effect("W", 0, 15) == "cross-shore"
    assert wind_effect("W", 260, 4) == "glassy"


def test_tide_turns_are_placed_within_the_hour():
    turns = tide_turns(TIMES, marine([1] * 24)["sea_level_height_msl"])
    assert [t["tide"] for t in turns] == ["low", "high", "low", "high"]
    assert turns[1]["time"] == f"{DAY}T06:27"


def test_windows_need_size_period_and_good_wind():
    heights = [0.3] * 10 + [1.2] * 6 + [3.5] * 8
    knots = [10] * 12 + [18] * 12
    directions = [90] * 12 + [0] * 12
    day = summarize_surf_day(
        SPOT, marine(heights), wind(knots, directions), f"{DAY}T08:10", f"{DAY}T19:20", ""
    )
    assert day["surfable_windows"] == [
        {"from": "10:00", "to": "12:00", "quality": "good", "waves_m": 1.2, "period_s": 10,
         "wind": "10 kn offshore"},
    ]  # fmt: skip
    assert day["waves_m"] == [0.3, 3.5]
    assert [t["tide"] for t in day["tides"]] == ["low", "high", "low", "high"]


@respx.mock
async def test_tool_merges_marine_and_wind_per_spot(ctx, monkeypatch):
    spots = [SPOT, SurfSpot(name="South Point", latitude=1.0, longitude=1.0, facing="SSW")]
    respx.get(MARINE_URL).respond(json=[{"hourly": marine([1.0] * 24)}] * 2)
    respx.get(FORECAST_URL).respond(
        json=[
            {
                "hourly": wind([5] * 24, [90] * 24),
                "daily": {"sunrise": [f"{DAY}T08:10"], "sunset": [f"{DAY}T19:20"]},
            }
        ]
        * 2
    )
    async with httpx.AsyncClient() as http:
        meteo = OpenMeteo(http, "Europe/Paris")
        monkeypatch.setattr(meteo, "now_hour", lambda: f"{DAY}T00:00")
        (tool,) = build_surf_tools(meteo, spots, model="best_match")
        result = await tool.handler({"days": 1}, ctx)

    assert [s["name"] for s in result["spots"]] == ["North Beach", "South Point"]
    window = result["spots"][0]["days"][0]["surfable_windows"][0]
    assert (window["from"], window["to"], window["wind"]) == ("08:00", "20:00", "5 kn glassy")


def test_unknown_facing_is_rejected_at_startup():
    with pytest.raises(ValueError):
        build_surf_tools(None, [SurfSpot(name="x", latitude=0, longitude=0, facing="WEST")],
                         model="best_match")  # fmt: skip
