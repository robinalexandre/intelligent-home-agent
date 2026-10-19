"""Driving time from home, to tell the user when to leave for an appointment."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.calendar import place_key

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org"
# Nominatim's usage policy requires an identifying User-Agent.
USER_AGENT = "intelligent-home-agent/0.1 (self-hosted voice assistant)"
MAX_DESTINATION_LENGTH = 200


class Router:
    """Geocoding with Nominatim and driving routes with OSRM (OpenStreetMap, no key)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        language: str,
        geocoding_url: str = NOMINATIM_URL,
        routing_url: str = OSRM_URL,
    ) -> None:
        self._http = http
        self._language = language
        self._geocoding_url = geocoding_url
        self._routing_url = routing_url.rstrip("/")

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._http.get(url, params=params, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"Routing service unavailable: {exc}") from exc
        return resp.json()

    async def geocode(self, place: str, near: tuple[float, float]) -> dict[str, Any]:
        parts = [p.strip() for p in re.split(r"[\n,;]+", place) if p.strip()]
        candidates = [", ".join(parts)]
        # Calendar locations often start with a venue name the geocoder doesn't know.
        if len(parts) >= 3:
            candidates.append(", ".join(parts[1:]))
        lat, lon = near
        for query in candidates:
            results = await self._get(
                self._geocoding_url,
                {
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "accept-language": self._language,
                    "viewbox": f"{lon - 1},{lat + 1},{lon + 1},{lat - 1}",
                },
            )
            if results:
                found = results[0]
                name = ", ".join(found.get("display_name", query).split(", ")[:3])
                return {
                    "name": name,
                    "latitude": float(found["lat"]),
                    "longitude": float(found["lon"]),
                }
        raise ToolError(f"Address not found: {place}")

    async def drive(
        self, origin: tuple[float, float], destination: tuple[float, float]
    ) -> tuple[float, float]:
        """Driving duration in seconds and distance in meters."""
        coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        data = await self._get(
            f"{self._routing_url}/route/v1/driving/{coords}", {"overview": "false"}
        )
        routes = data.get("routes") or []
        if data.get("code") != "Ok" or not routes:
            raise ToolError("No driving route found.")
        return routes[0]["duration"], routes[0]["distance"]


def build_travel_tools(
    router: Router,
    *,
    home: tuple[float, float] | None,
    timezone: str,
    margin_minutes: int,
) -> list[Tool]:
    if home is None:
        return []
    tz = ZoneInfo(timezone)

    async def get_travel_time(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        destination = str(args.get("destination") or "").strip()
        has_coordinates = args.get("latitude") is not None and args.get("longitude") is not None
        if ctx.tainted:
            # The destination leaves the server: after reading third-party content, only
            # places that came from the user's own calendar may go out.
            sent = (
                place_key(args["latitude"], args["longitude"]) if has_coordinates else destination
            )
            if sent not in ctx.calendar_places:
                raise ToolError(
                    "In this request, travel time only works for the place of a calendar "
                    "event. Tell the user to ask again in a separate request."
                )
        destination = destination[:MAX_DESTINATION_LENGTH]
        if has_coordinates:
            place = {
                "name": destination or "destination",
                "latitude": float(args["latitude"]),
                "longitude": float(args["longitude"]),
            }
        elif destination:
            place = await router.geocode(destination, near=home)
        else:
            raise ToolError("Give a destination address or coordinates.")

        seconds, meters = await router.drive(home, (place["latitude"], place["longitude"]))
        minutes = math.ceil(seconds / 60)
        result: dict[str, Any] = {
            "destination": place["name"],
            "distance_km": round(meters / 1000, 1),
            "drive_minutes": minutes,
            "live_traffic": False,
        }
        if arrive_by := args.get("arrive_by"):
            try:
                arrival = datetime.fromisoformat(arrive_by)
            except ValueError as exc:
                raise ToolError("arrive_by must be an ISO date-time") from exc
            if arrival.tzinfo is None:
                arrival = arrival.replace(tzinfo=tz)
            leave = arrival - timedelta(minutes=minutes + margin_minutes)
            result["leave_by"] = leave.astimezone(tz).isoformat(timespec="minutes")
            result["margin_minutes"] = margin_minutes
        return result

    return [
        Tool(
            name="get_travel_time",
            description=(
                "Driving time from home to a place (no live traffic) and, given the "
                "appointment time, when to leave including a safety margin. Pass the "
                "coordinates of a calendar event when it has them, otherwise its address."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "destination": {"type": "string", "description": "Address or place."},
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "arrive_by": {
                        "type": "string",
                        "description": "Appointment start, local ISO date-time.",
                    },
                },
            },
            handler=get_travel_time,
        )
    ]
