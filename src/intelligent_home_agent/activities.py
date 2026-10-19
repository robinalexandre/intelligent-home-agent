"""Outdoor activities and the forecast data each one needs.

Three kinds of activities:
- ``wind``: wind sports, judged per spot (strength, gusts, direction): get_wind_forecast.
- ``surf``: judged per beach (waves, period, wind effect, tides): get_surf_forecast.
- ``weather``: everything else, judged on the weather at home or in a named place
  (rain, wind, temperature, UV, snow, visibility): get_outdoor_forecast.

Standard library only: scripts/setup.py imports this module before anything is installed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Kind = Literal["wind", "surf", "weather"]

# Forecast data an activity can need, with a short label for prompts and the setup script.
FORECASTS = {
    "wind": "wind speed, gusts and direction",
    "waves": "wave height and period",
    "tides": "tide times",
    "rain": "rain",
    "temperature": "temperature",
    "uv": "UV index",
    "snow": "snowfall",
    "visibility": "visibility",
}


@dataclass(frozen=True)
class Activity:
    key: str
    kind: Kind
    needs: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    # Limits for a "good" hour, used by weather-kind activities (None: not checked).
    max_rain_pct: int | None = None
    max_wind_kmh: float | None = None
    min_temp_c: float | None = None
    max_temp_c: float | None = None
    min_visibility_m: float | None = None


CATALOG: dict[str, Activity] = {
    a.key: a
    for a in [
        Activity(
            "surf",
            "surf",
            ("waves", "tides", "wind"),
            ("surfing", "bodyboard", "bodyboarding", "longboard", "bodysurf"),
        ),
        Activity(
            "wingfoil",
            "wind",
            ("wind", "tides"),
            ("wing", "wingfoiling", "wing foil", "wingsurf", "foil"),
        ),
        Activity(
            "kitesurf",
            "wind",
            ("wind", "tides"),
            ("kite", "kitesurfing", "kiteboarding", "kiteboard"),
        ),
        Activity("windsurf", "wind", ("wind",), ("windsurfing", "planche a voile")),
        Activity("sailing", "wind", ("wind", "waves"), ("sail", "voile", "dinghy", "catamaran")),
        Activity(
            "cycling",
            "weather",
            ("rain", "wind", "temperature"),
            ("bike", "biking", "cycle", "velo", "cyclisme", "vtt", "mtb", "gravel", "road bike"),
            max_rain_pct=30,
            max_wind_kmh=35,
            min_temp_c=5,
            max_temp_c=32,
        ),
        Activity(
            "running",
            "weather",
            ("rain", "temperature", "uv"),
            ("run", "jogging", "jog", "course a pied", "footing", "trail"),
            max_rain_pct=50,
            min_temp_c=-5,
            max_temp_c=28,
        ),
        Activity(
            "hiking",
            "weather",
            ("rain", "temperature", "uv", "visibility"),
            ("hike", "walk", "walking", "randonnee", "rando", "marche", "trekking"),
            max_rain_pct=30,
            max_wind_kmh=50,
            min_temp_c=0,
            max_temp_c=30,
            min_visibility_m=5000,
        ),
        Activity(
            "skiing",
            "weather",
            ("snow", "temperature", "visibility", "wind"),
            ("ski", "snowboard", "snowboarding", "ski touring", "randonnee a ski"),
            max_rain_pct=60,
            max_wind_kmh=50,
            max_temp_c=8,
            min_visibility_m=1000,
        ),
        Activity(
            "climbing",
            "weather",
            ("rain", "temperature"),
            ("climb", "escalade", "bouldering", "bloc", "via ferrata"),
            max_rain_pct=20,
            min_temp_c=5,
            max_temp_c=30,
        ),
        Activity(
            "golf",
            "weather",
            ("rain", "wind", "temperature"),
            (),
            max_rain_pct=30,
            max_wind_kmh=30,
            min_temp_c=5,
        ),
        Activity(
            "outdoors",
            "weather",
            ("rain", "temperature", "uv"),
            (
                "outside",
                "go out",
                "i go out a lot",
                "out a lot",
                "dehors",
                "sortir",
                "garden",
                "gardening",
                "jardin",
                "jardinage",
                "picnic",
                "terrace",
                "promenade",
            ),
            max_rain_pct=40,
            min_temp_c=0,
            max_temp_c=32,
        ),
    ]
}


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _names(activity: Activity) -> list[str]:
    return [activity.key, *activity.aliases]


def match_activities(text: str) -> tuple[list[str], list[str]]:
    """Activities named in free text, in order, and the fragments nothing matched.

    "surf, wing and some cycling" -> (["surf", "wingfoil", "cycling"], []).
    """
    found: list[str] = []
    unknown: list[str] = []
    fragments = [f.strip() for f in re.split(r",|;|/|\+|\band\b|\bet\b|\n", normalize(text))]
    for fragment in filter(None, fragments):
        hits = []
        for activity in CATALOG.values():
            for name in sorted(_names(activity), key=len, reverse=True):
                match = re.search(rf"\b{re.escape(normalize(name))}\b", fragment)
                if match:
                    hits.append((match.start(), activity.key))
                    break
        if not hits:
            unknown.append(fragment)
        for _, key in sorted(hits):
            if key not in found:
                found.append(key)
    return found, unknown


def resolve(keys: list[str]) -> list[Activity]:
    """Activities from setting values; names and aliases both work."""
    resolved = []
    for raw in keys:
        matches, _ = match_activities(raw)
        if not matches:
            raise ValueError(f"Unknown outdoor activity {raw!r}; known: {', '.join(CATALOG)}")
        for key in matches:
            if CATALOG[key] not in resolved:
                resolved.append(CATALOG[key])
    return resolved


def forecasts_for(activities: list[Activity]) -> list[str]:
    needs: list[str] = []
    for activity in activities:
        needs.extend(n for n in activity.needs if n not in needs)
    return needs
