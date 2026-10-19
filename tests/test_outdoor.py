import httpx
import pytest
import respx

from intelligent_home_agent.activities import CATALOG
from intelligent_home_agent.tools import ToolError, build_outdoor_tools
from intelligent_home_agent.tools.outdoor import summarize_activity_day
from intelligent_home_agent.tools.weather import FORECAST_URL, OpenMeteo

DAY = "2026-10-14"
TIMES = [f"{DAY}T{h:02d}:00" for h in range(24)]


def hourly(rain=None, wind=None, temp=None, **extra):
    return {
        "time": TIMES,
        "temperature_2m": temp or [15] * 24,
        "precipitation_probability": rain or [0] * 24,
        "wind_speed_10m": wind or [10] * 24,
        "wind_gusts_10m": [20] * 24,
        "uv_index": [3] * 24,
        "snowfall": [0] * 24,
        "visibility": [20000] * 24,
        **extra,
    }


def test_windows_skip_rainy_and_windy_hours():
    rain = [0] * 24
    wind = [10] * 24
    rain[12] = rain[13] = 80
    wind[16] = 60
    day = summarize_activity_day(
        CATALOG["cycling"], hourly(rain, wind), f"{DAY}T08:10", f"{DAY}T19:05", f"{DAY}T09:00"
    )
    assert [(w["from"], w["to"]) for w in day["good_windows"]] == [
        ("09:00", "12:00"),
        ("14:00", "16:00"),
        ("17:00", "20:00"),
    ]
    assert day["limited_by"] == {"rain": 2, "wind": 1}
    assert day["good_windows"][0]["temp_c"] == [15, 15]
    assert "uv_max" not in day


def test_activity_specific_extras():
    snow = [0.5] * 24
    day = summarize_activity_day(
        CATALOG["skiing"],
        hourly(temp=[-2] * 24, snowfall=snow, visibility=[800] * 12 + [5000] * 12),
        f"{DAY}T08:10",
        f"{DAY}T17:00",
        "",
    )
    assert day["snowfall_cm"] == 5.0  # 08:00 to 17:00
    assert day["min_visibility_m"] == 800
    assert day["limited_by"] == {"visibility": 4}
    running = summarize_activity_day(
        CATALOG["running"], hourly(), f"{DAY}T08:10", f"{DAY}T17:00", ""
    )
    assert running["uv_max"] == 3


def test_no_daylight_left():
    day = summarize_activity_day(
        CATALOG["golf"], hourly(), f"{DAY}T08:10", f"{DAY}T19:05", f"{DAY}T21:00"
    )
    assert day == {"date": DAY, "daylight_hours_left": False}


def test_only_weather_activities_get_the_tool():
    assert build_outdoor_tools(None, [CATALOG["surf"]], home=None, language="en") == []


@respx.mock
async def test_tool_uses_home_and_lists_limits(ctx, monkeypatch):
    route = respx.get(FORECAST_URL).respond(
        json={
            "hourly": hourly(),
            "daily": {"sunrise": [f"{DAY}T08:10"], "sunset": [f"{DAY}T19:05"]},
        }
    )
    async with httpx.AsyncClient() as http:
        meteo = OpenMeteo(http, "UTC")
        monkeypatch.setattr(meteo, "now_hour", lambda: f"{DAY}T00:00")
        activities = [CATALOG["surf"], CATALOG["hiking"], CATALOG["golf"]]
        (tool,) = build_outdoor_tools(meteo, activities, home=(0.0, 0.0), language="en")
        assert tool.input_schema["properties"]["activity"]["enum"] == ["hiking", "golf"]

        result = await tool.handler({"activity": "golf", "days": 1}, ctx)
        with pytest.raises(ToolError, match="surf"):
            await tool.handler({"activity": "surf"}, ctx)

    assert route.calls.last.request.url.params["latitude"] == "0.0"
    assert result["limits"] == {"max_rain_pct": 30, "max_wind_kmh": 30, "min_temp_c": 5}
    assert result["days"][0]["good_windows"][0]["from"] == "08:00"
