"""Weather windows for outdoor activities that don't need a spot (cycling, hiking, golf...).

Each activity in ``activities.CATALOG`` with kind "weather" has limits (rain probability,
wind, temperature, visibility). An hour is good when every limit it has passes; the tool
returns the daylight windows of good hours, plus the day's UV and snowfall when the
activity cares about them.
"""

from __future__ import annotations

from typing import Any

from intelligent_home_agent.activities import Activity
from intelligent_home_agent.config import HomeNow, Place, home_now
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.weather import OpenMeteo, is_daylight, window_end

HOURLY = (
    "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m,"
    "uv_index,snowfall,visibility"
)


def hour_problems(activity: Activity, hour: dict[str, Any]) -> list[str]:
    """Why an hour doesn't suit the activity (empty: it does).

    Missing data is not held against the hour.
    """
    problems = []
    rain, wind, temp = hour.get("rain_pct"), hour.get("wind_kmh"), hour.get("temp_c")
    visibility = hour.get("visibility_m")
    if activity.max_rain_pct is not None and rain is not None and rain > activity.max_rain_pct:
        problems.append("rain")
    if activity.max_wind_kmh is not None and wind is not None and wind > activity.max_wind_kmh:
        problems.append("wind")
    if activity.min_temp_c is not None and temp is not None and temp < activity.min_temp_c:
        problems.append("cold")
    if activity.max_temp_c is not None and temp is not None and temp > activity.max_temp_c:
        problems.append("heat")
    if (
        activity.min_visibility_m is not None
        and visibility is not None
        and visibility < activity.min_visibility_m
    ):
        problems.append("visibility")
    return problems


def summarize_activity_day(
    activity: Activity,
    hourly: dict[str, list[Any]],
    sunrise: str,
    sunset: str,
    not_before: str,
) -> dict[str, Any]:
    day = sunrise[:10]

    def value(name: str, i: int) -> Any:
        values = hourly.get(name)
        return values[i] if values and i < len(values) else None

    hours = []
    for i, t in enumerate(hourly["time"]):
        if not is_daylight(t, sunrise, sunset, not_before):
            continue
        hour = {
            "time": t,
            "temp_c": value("temperature_2m", i),
            "rain_pct": value("precipitation_probability", i),
            "wind_kmh": value("wind_speed_10m", i),
            "gusts_kmh": value("wind_gusts_10m", i),
            "uv": value("uv_index", i),
            "snow_cm": value("snowfall", i),
            "visibility_m": value("visibility", i),
        }
        hour["problems"] = hour_problems(activity, hour)
        hours.append(hour)

    if not hours:
        return {"date": day, "daylight_hours_left": False}

    windows: list[list[dict[str, Any]]] = []
    for index, hour in enumerate(hours):
        if hour["problems"]:
            continue
        if windows and hours[index - 1] is windows[-1][-1]:
            windows[-1].append(hour)
        else:
            windows.append([hour])

    limited_by: dict[str, int] = {}
    for hour in hours:
        for problem in hour["problems"]:
            limited_by[problem] = limited_by.get(problem, 0) + 1

    def numbers(name: str, group: list[dict[str, Any]]) -> list[float]:
        return [h[name] for h in group if h[name] is not None]

    summary: dict[str, Any] = {
        "date": day,
        "good_windows": [
            {
                "from": w[0]["time"][11:16],
                "to": window_end(w[-1]["time"]),
                "temp_c": [round(min(temps)), round(max(temps))]
                if (temps := numbers("temp_c", w))
                else None,
                "max_rain_pct": max(numbers("rain_pct", w), default=None),
                "max_wind_kmh": round(max(numbers("wind_kmh", w), default=0)),
            }
            for w in windows
        ],
        # Daylight hours ruled out, by reason: "rain": 5 means five rainy hours.
        "limited_by": limited_by,
    }
    if "uv" in activity.needs and (uv := numbers("uv", hours)):
        summary["uv_max"] = round(max(uv), 1)
    if "snow" in activity.needs:
        summary["snowfall_cm"] = round(sum(numbers("snow_cm", hours)), 1)
    if "visibility" in activity.needs and (vis := numbers("visibility_m", hours)):
        summary["min_visibility_m"] = round(min(vis))
    return summary


def limits(activity: Activity) -> dict[str, float]:
    return {
        k: v
        for k, v in {
            "max_rain_pct": activity.max_rain_pct,
            "max_wind_kmh": activity.max_wind_kmh,
            "min_temp_c": activity.min_temp_c,
            "max_temp_c": activity.max_temp_c,
            "min_visibility_m": activity.min_visibility_m,
        }.items()
        if v is not None
    }


async def outdoor_days(
    meteo: OpenMeteo, activity: Activity, latitude: float, longitude: float, *, days: int
) -> list[dict[str, Any]]:
    """The good daylight windows of each day there, for get_outdoor_forecast and the
    activity alerts (activity_alerts.py)."""
    data = await meteo.forecast(
        latitude=latitude,
        longitude=longitude,
        forecast_days=days,
        hourly=HOURLY,
        daily="sunrise,sunset",
    )
    now = meteo.now_hour()
    daily = data["daily"]
    return [
        summarize_activity_day(activity, data["hourly"], sunrise, daily["sunset"][i], now)
        for i, sunrise in enumerate(daily["sunrise"])
    ]


def build_outdoor_tools(
    meteo: OpenMeteo,
    activities: list[Activity],
    *,
    home: Place | HomeNow | None,
    language: str,
) -> list[Tool]:
    by_key = {a.key: a for a in activities if a.kind == "weather"}
    if not by_key:
        return []
    where = home_now(home)

    async def get_outdoor_forecast(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        key = args.get("activity") or next(iter(by_key))
        activity = by_key.get(key)
        if activity is None:
            raise ToolError(f"Unknown activity {key}. Known: {list(by_key)}")
        days = min(max(int(args.get("days", 2)), 1), 7)
        if place := args.get("place"):
            location = await meteo.geocode(place, language)
        elif (here := where()) is not None:
            location = {"name": "home", "latitude": here[0], "longitude": here[1]}
        else:
            raise ToolError("The home location is not configured: ask which place.")
        return {
            "activity": activity.key,
            "place": location,
            "limits": limits(activity),
            "days": await outdoor_days(
                meteo, activity, location["latitude"], location["longitude"], days=days
            ),
        }

    return [
        Tool(
            name="get_outdoor_forecast",
            description=(
                "Daylight hours with suitable weather for one of the user's outdoor "
                "activities, at home by default or in a named place, for 1 to 7 days: good "
                "windows, what rules out the other hours (rain, wind, cold, heat, "
                f"visibility), and UV or snowfall when relevant. Activities: {', '.join(by_key)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "activity": {"type": "string", "enum": list(by_key)},
                    "place": {"type": "string", "description": "Place name; omit for home."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 7},
                },
            },
            handler=get_outdoor_forecast,
        )
    ]
