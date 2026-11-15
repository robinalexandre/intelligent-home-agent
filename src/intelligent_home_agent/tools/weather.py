"""Weather and wind forecasts from Open-Meteo (free, no API key)."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.config import HomeNow, Place, WindSpot, home_now, in_season
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
NEXT_HOURS = 12

_WMO_CODES = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}

COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]  # fmt: skip
# Half the width of an 8-point sector, so "SW" accepts winds from SSW to WSW included.
_DIRECTION_TOLERANCE = 22.5


def compass_point(degrees: float) -> str:
    return COMPASS_POINTS[round(degrees / 22.5) % 16]


def angle_between(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def compass_degrees(point: str) -> float:
    return COMPASS_POINTS.index(point.upper()) * 22.5


def direction_matches(degrees: float, accepted: list[str]) -> bool:
    if not accepted:
        return True
    return any(angle_between(degrees, compass_degrees(p)) <= _DIRECTION_TOLERANCE for p in accepted)


def _mean_direction(degrees: list[float]) -> float:
    x = sum(math.cos(math.radians(d)) for d in degrees)
    y = sum(math.sin(math.radians(d)) for d in degrees)
    return math.degrees(math.atan2(y, x)) % 360


def _conditions(code: int | None) -> str | None:
    return None if code is None else _WMO_CODES.get(code, f"code {code}")


def _hour(iso: str) -> str:
    return iso[11:16]


def is_daylight(t: str, sunrise: str, sunset: str, not_before: str) -> bool:
    return t.startswith(sunrise[:10]) and sunrise[:13] <= t <= sunset and t >= not_before


def window_end(last_hour: str) -> str:
    return f"{int(_hour(last_hour)[:2]) + 1:02d}:00"


class OpenMeteo:
    def __init__(self, http: httpx.AsyncClient, timezone: str) -> None:
        self._http = http
        self.timezone = timezone

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._http.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"Weather service unavailable: {exc}") from exc
        return resp.json()

    async def geocode(self, place: str, language: str) -> dict[str, Any]:
        data = await self._get(
            GEOCODING_URL, {"name": place, "count": 1, "language": language, "format": "json"}
        )
        results = data.get("results") or []
        if not results:
            raise ToolError(f"Unknown place: {place}")
        found = results[0]
        return {
            "name": found["name"],
            "country": found.get("country"),
            "latitude": found["latitude"],
            "longitude": found["longitude"],
        }

    async def forecast(self, **params: Any) -> Any:
        return await self._get(FORECAST_URL, {"timezone": self.timezone, **params})

    async def marine(self, **params: Any) -> Any:
        return await self._get(MARINE_URL, {"timezone": self.timezone, **params})

    def now_hour(self) -> str:
        return datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%dT%H:00")


def build_weather_tools(
    meteo: OpenMeteo,
    *,
    home: Place | HomeNow | None,
    language: str,
) -> list[Tool]:
    where = home_now(home)

    async def get_weather(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        days = min(max(int(args.get("days", 2)), 1), 7)
        if place := args.get("place"):
            location = await meteo.geocode(place, language)
        elif (here := where()) is not None:
            location = {"name": "home", "latitude": here[0], "longitude": here[1]}
        else:
            raise ToolError("The home location is not configured: ask which city.")

        data = await meteo.forecast(
            latitude=location["latitude"],
            longitude=location["longitude"],
            forecast_days=days,
            current="temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
            hourly="temperature_2m,precipitation_probability,weather_code",
            daily=(
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,precipitation_sum,"
                "wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset"
            ),
        )
        current = data.get("current", {})
        hourly = data.get("hourly", {})
        daily = data.get("daily", {})
        now = meteo.now_hour()
        next_hours = [
            {
                "time": _hour(t),
                "temperature_c": hourly["temperature_2m"][i],
                "rain_probability_pct": hourly["precipitation_probability"][i],
                "conditions": _conditions(hourly["weather_code"][i]),
            }
            for i, t in enumerate(hourly.get("time", []))
            if t >= now
        ][:NEXT_HOURS]
        return {
            "place": location,
            "current": {
                "temperature_c": current.get("temperature_2m"),
                "feels_like_c": current.get("apparent_temperature"),
                "conditions": _conditions(current.get("weather_code")),
                "wind_kmh": current.get("wind_speed_10m"),
                "precipitation_mm": current.get("precipitation"),
            },
            "next_hours": next_hours,
            "days": [
                {
                    "date": d,
                    "conditions": _conditions(daily["weather_code"][i]),
                    "min_c": daily["temperature_2m_min"][i],
                    "max_c": daily["temperature_2m_max"][i],
                    "rain_probability_pct": daily["precipitation_probability_max"][i],
                    "rain_mm": daily["precipitation_sum"][i],
                    "wind_max_kmh": daily["wind_speed_10m_max"][i],
                    "gusts_max_kmh": daily["wind_gusts_10m_max"][i],
                    "sunrise": _hour(daily["sunrise"][i]),
                    "sunset": _hour(daily["sunset"][i]),
                }
                for i, d in enumerate(daily.get("time", []))
            ],
        }

    return [
        Tool(
            name="get_weather",
            description=(
                "Weather now, for the next hours and the next days (1 to 7), at home by "
                "default or in a named city."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "City name; omit for home."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 7},
                },
            },
            handler=get_weather,
        )
    ]


def summarize_spot_day(
    spot: WindSpot,
    hourly: dict[str, list[Any]],
    sunrise: str,
    sunset: str,
    not_before: str,
) -> dict[str, Any]:
    """Daylight wind summary for one spot and one day, with the rideable windows."""
    day = sunrise[:10]
    hours = []
    for i, t in enumerate(hourly["time"]):
        speed = hourly["wind_speed_10m"][i]
        direction = hourly["wind_direction_10m"][i]
        if not is_daylight(t, sunrise, sunset, not_before):
            continue
        if speed is None or direction is None:
            continue
        hours.append((t, speed, hourly["wind_gusts_10m"][i] or speed, direction))

    if not hours:
        return {"date": day, "daylight_hours_left": False}

    windows: list[list[tuple[str, float, float, float]]] = []
    previous_index = None
    for index, hour in enumerate(hours):
        if hour[1] < spot.min_knots or not direction_matches(hour[3], spot.directions):
            continue
        if previous_index is not None and previous_index == index - 1:
            windows[-1].append(hour)
        else:
            windows.append([hour])
        previous_index = index

    peak = max(hours, key=lambda h: h[1])
    return {
        "date": day,
        "max_knots": round(peak[1]),
        "max_gust_knots": round(max(h[2] for h in hours)),
        "direction_at_max": compass_point(peak[3]),
        "rideable_windows": [
            {
                "from": _hour(w[0][0]),
                "to": window_end(w[-1][0]),
                "avg_knots": round(sum(h[1] for h in w) / len(w)),
                "max_gust_knots": round(max(h[2] for h in w)),
                "direction": compass_point(_mean_direction([h[3] for h in w])),
            }
            for w in windows
        ],
    }


def named_spots(spots: list[Any], name: str) -> list[Any]:
    """The spot of that name, or the only one whose name holds it ("North Beach" for
    "North Beach, east end")."""
    wanted = name.casefold().strip()
    exact = [s for s in spots if s.name.casefold() == wanted]
    partial = [s for s in spots if wanted in s.name.casefold()]
    return exact or (partial if len(partial) == 1 else [])


def spots_now(spots: list[Any], today: date, name: str | None = None) -> list[Any]:
    """The named spot, else those in season today (all of them when none is)."""
    if name:
        selected = named_spots(spots, name)
        if not selected:
            raise ToolError(f"Unknown spot {name}. Known spots: {[s.name for s in spots]}")
        return selected
    return [s for s in spots if in_season(s, today)] or spots


async def wind_report(
    meteo: OpenMeteo, spots: list[WindSpot], *, days: int, model: str
) -> list[dict[str, Any]]:
    """Each spot's daylight wind and rideable windows, for get_wind_forecast and the
    activity alerts (activity_alerts.py)."""
    data = await meteo.forecast(
        latitude=",".join(str(s.latitude) for s in spots),
        longitude=",".join(str(s.longitude) for s in spots),
        forecast_days=days,
        hourly="wind_speed_10m,wind_gusts_10m,wind_direction_10m",
        daily="sunrise,sunset",
        wind_speed_unit="kn",
        models=model,
    )
    # Open-Meteo returns a list for several coordinates, a single object for one.
    locations = data if isinstance(data, list) else [data]
    now = meteo.now_hour()
    return [
        {
            "name": spot.name,
            "min_knots": spot.min_knots,
            "good_directions": spot.directions or "any",
            "days": [
                summarize_spot_day(
                    spot, location["hourly"], sunrise, location["daily"]["sunset"][i], now
                )
                for i, sunrise in enumerate(location["daily"]["sunrise"])
            ],
        }
        for spot, location in zip(spots, locations, strict=True)
    ]


def build_wind_tools(meteo: OpenMeteo, spots: list[WindSpot], *, model: str) -> list[Tool]:
    for spot in spots:
        unknown = [d for d in spot.directions if d.upper() not in COMPASS_POINTS]
        if unknown:
            raise ValueError(f"Wind spot {spot.name}: unknown directions {unknown}")
    if not spots:
        return []

    async def get_wind_forecast(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        days = min(max(int(args.get("days", 2)), 1), 3)
        today = date.fromisoformat(meteo.now_hour()[:10])
        # The spots in season (near where the user is now), unless they name another one.
        selected = spots_now(spots, today, args.get("spot"))
        return {
            "unit": "knots",
            "source": {"name": "Open-Meteo", "model": model},
            "spots": await wind_report(meteo, selected, days=days, model=model),
        }

    return [
        Tool(
            name="get_wind_forecast",
            description=(
                "Wind forecast in knots for the user's wind-sport spots (wingfoil, kitesurf, "
                "windsurf, sailing...), during daylight, with the time windows where the wind "
                "is strong enough and from a good direction for each spot. Without a spot, "
                "those in season now. Known spots: "
                f"{', '.join(s.name for s in spots)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "spot": {"type": "string", "description": "One spot name; omit for all."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 3},
                },
            },
            handler=get_wind_forecast,
        )
    ]
