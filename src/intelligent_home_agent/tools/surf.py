"""Surf forecast and tide times from Open-Meteo's marine and weather models (no key)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from intelligent_home_agent.config import SurfSpot
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.weather import (
    COMPASS_POINTS,
    OpenMeteo,
    angle_between,
    compass_degrees,
    compass_point,
    is_daylight,
    window_end,
)

GLASSY_KNOTS = 6
MAX_CROSS_SHORE_KNOTS = 12
MIN_PERIOD_SECONDS = 8
_SHORE_TOLERANCE = 45


def wind_effect(facing: str, direction: float, knots: float) -> str:
    if knots < GLASSY_KNOTS:
        return "glassy"
    sea = compass_degrees(facing)
    if angle_between(direction, (sea + 180) % 360) <= _SHORE_TOLERANCE:
        return "offshore"
    if angle_between(direction, sea) <= _SHORE_TOLERANCE:
        return "onshore"
    return "cross-shore"


def rate_hour(spot: SurfSpot, height: float, period: float, wind: str, knots: float) -> str:
    if height < spot.min_wave_m:
        return "flat"
    if height > spot.max_wave_m:
        return "too big"
    if period < MIN_PERIOD_SECONDS or wind == "onshore":
        return "poor"
    if wind == "cross-shore":
        return "fair" if knots <= MAX_CROSS_SHORE_KNOTS else "poor"
    return "good"


def tide_turns(times: list[str], levels: list[float | None]) -> list[dict[str, str]]:
    """High and low tides from hourly sea levels, placed within the hour."""
    turns = []
    for i in range(1, len(levels) - 1):
        a, b, c = levels[i - 1], levels[i], levels[i + 1]
        if a is None or b is None or c is None:
            continue
        if b > a and b >= c:
            kind = "high"
        elif b < a and b <= c:
            kind = "low"
        else:
            continue
        # Vertex of the parabola through the three hourly points.
        curvature = a - 2 * b + c
        shift = 0.5 * (a - c) / curvature if curvature else 0.0
        when = datetime.fromisoformat(times[i]) + timedelta(hours=shift)
        turns.append({"time": when.strftime("%Y-%m-%dT%H:%M"), "tide": kind})
    return turns


def summarize_surf_day(
    spot: SurfSpot,
    marine: dict[str, list[Any]],
    wind: dict[str, list[Any]],
    sunrise: str,
    sunset: str,
    not_before: str,
) -> dict[str, Any]:
    day = sunrise[:10]
    tides = [
        {"time": turn["time"][11:], "tide": turn["tide"]}
        for turn in tide_turns(marine["time"], marine.get("sea_level_height_msl", []))
        if turn["time"].startswith(day)
    ]
    wind_at = {t: i for i, t in enumerate(wind["time"])}
    hours = []
    for i, t in enumerate(marine["time"]):
        j = wind_at.get(t)
        height, period = marine["wave_height"][i], marine["wave_period"][i]
        if j is None or height is None or period is None:
            continue
        if not is_daylight(t, sunrise, sunset, not_before):
            continue
        knots, direction = wind["wind_speed_10m"][j], wind["wind_direction_10m"][j]
        if knots is None or direction is None:
            continue
        effect = wind_effect(spot.facing, direction, knots)
        hours.append(
            {
                "time": t,
                "height": height,
                "period": period,
                "swell_from": marine["wave_direction"][i],
                "wind_knots": knots,
                "wind": effect,
                "rating": rate_hour(spot, height, period, effect, knots),
            }
        )

    if not hours:
        return {"date": day, "daylight_hours_left": False, "tides": tides}

    windows: list[list[dict[str, Any]]] = []
    for index, hour in enumerate(hours):
        if hour["rating"] not in ("good", "fair"):
            continue
        if windows and hours[index - 1] is windows[-1][-1]:
            windows[-1].append(hour)
        else:
            windows.append([hour])

    biggest = max(hours, key=lambda h: h["height"])
    return {
        "date": day,
        "waves_m": [round(min(h["height"] for h in hours), 1), round(biggest["height"], 1)],
        "period_s": round(biggest["period"]),
        "swell_from": compass_point(biggest["swell_from"] or 0),
        "tides": tides,
        "surfable_windows": [
            {
                "from": w[0]["time"][11:16],
                "to": window_end(w[-1]["time"]),
                "quality": "good" if all(h["rating"] == "good" for h in w) else "fair",
                "waves_m": round(max(h["height"] for h in w), 1),
                "period_s": round(max(h["period"] for h in w)),
                "wind": f"{round(max(h['wind_knots'] for h in w))} kn {w[0]['wind']}",
            }
            for w in windows
        ],
    }


def build_surf_tools(meteo: OpenMeteo, spots: list[SurfSpot], *, model: str) -> list[Tool]:
    for spot in spots:
        if spot.facing.upper() not in COMPASS_POINTS:
            raise ValueError(f"Surf spot {spot.name}: unknown facing {spot.facing}")
    if not spots:
        return []

    async def get_surf_forecast(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        days = min(max(int(args.get("days", 2)), 1), 3)
        selected = spots
        if name := args.get("spot"):
            selected = [s for s in spots if s.name.casefold() == name.casefold()]
            if not selected:
                raise ToolError(f"Unknown spot {name}. Known spots: {[s.name for s in spots]}")

        coords = {
            "latitude": ",".join(str(s.latitude) for s in selected),
            "longitude": ",".join(str(s.longitude) for s in selected),
            "forecast_days": days,
        }
        marine, weather = await asyncio.gather(
            meteo.marine(
                **coords,
                hourly="wave_height,wave_period,wave_direction,sea_level_height_msl",
            ),
            meteo.forecast(
                **coords,
                hourly="wind_speed_10m,wind_direction_10m",
                daily="sunrise,sunset",
                wind_speed_unit="kn",
                models=model,
            ),
        )
        # Open-Meteo returns a list for several coordinates, a single object for one.
        marine = marine if isinstance(marine, list) else [marine]
        weather = weather if isinstance(weather, list) else [weather]
        now = meteo.now_hour()
        return {
            "units": {"waves": "m (significant height)", "wind": "knots"},
            "tides_note": "model estimate, about half an hour of uncertainty",
            "spots": [
                {
                    "name": spot.name,
                    "facing": spot.facing,
                    "days": [
                        summarize_surf_day(
                            spot,
                            sea["hourly"],
                            air["hourly"],
                            sunrise,
                            air["daily"]["sunset"][i],
                            now,
                        )
                        for i, sunrise in enumerate(air["daily"]["sunrise"])
                    ],
                }
                for spot, sea, air in zip(selected, marine, weather, strict=True)
            ],
        }

    return [
        Tool(
            name="get_surf_forecast",
            description=(
                "Surf report for the user's surf spots: wave height and period, wind effect "
                "(offshore, onshore...), the daylight windows worth surfing, and high and low "
                "tide times, which also help for tide-dependent wind spots. Known spots: "
                f"{', '.join(s.name for s in spots)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "spot": {"type": "string", "description": "One spot name; omit for all."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 3},
                },
            },
            handler=get_surf_forecast,
        )
    ]
