import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools.travel import NOMINATIM_URL, OSRM_URL, Router, build_travel_tools

# Public places: from the Louvre to the Eiffel Tower.
HOME = (48.8606, 2.3376)
ROUTE_URL = f"{OSRM_URL}/route/v1/driving/2.3376,48.8606;2.2945,48.8584"
ROUTE = {"code": "Ok", "routes": [{"duration": 1130.0, "distance": 15420.0}]}
FOUND = [{"lat": "48.8584", "lon": "2.2945", "display_name": "Eiffel Tower, Champ de Mars, X, Y"}]


@pytest.fixture
async def tool():
    async with httpx.AsyncClient() as http:
        (travel,) = build_travel_tools(
            Router(http, language="fr"), home=HOME, timezone="Europe/Paris", margin_minutes=10
        )
        yield travel


@respx.mock
async def test_leave_by_from_coordinates(tool, ctx):
    respx.get(ROUTE_URL).respond(json=ROUTE)

    result = await tool.handler(
        {"latitude": 48.8584, "longitude": 2.2945, "arrive_by": "2026-10-15T09:00"}, ctx
    )

    assert result["drive_minutes"] == 19
    assert result["distance_km"] == 15.4
    assert result["leave_by"] == "2026-10-15T08:31+02:00"
    assert result["live_traffic"] is False


@respx.mock
async def test_geocoder_retries_without_the_venue_name(tool, ctx):
    geocoder = respx.get(NOMINATIM_URL).mock(
        side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=FOUND)]
    )
    respx.get(ROUTE_URL).respond(json=ROUTE)

    result = await tool.handler({"destination": "Alex's place\n1 Example Street\nSpringfield"}, ctx)

    assert result["destination"] == "Eiffel Tower, Champ de Mars, X"
    queries = [call.request.url.params["q"] for call in geocoder.calls]
    assert queries == [
        "Alex's place, 1 Example Street, Springfield",
        "1 Example Street, Springfield",
    ]
    assert "intelligent-home-agent" in geocoder.calls[0].request.headers["user-agent"]


@respx.mock
async def test_unknown_address_is_reported_to_the_model(tool, ctx):
    respx.get(NOMINATIM_URL).respond(json=[])
    with pytest.raises(ToolError, match="not found"):
        await tool.handler({"destination": "nowhere"}, ctx)


@respx.mock
async def test_after_private_data_only_calendar_places_go_out(tool, ctx):
    respx.get(ROUTE_URL).respond(json=ROUTE)
    ctx.tainted = True
    ctx.calendar_places = {"48.8584,2.2945"}

    with pytest.raises(ToolError, match="calendar event"):
        await tool.handler({"destination": "text copied from an email"}, ctx)
    ctx.calendar_places.add("1 Example Street")
    with pytest.raises(ToolError, match="calendar event"):
        await tool.handler(
            {"destination": "1 Example Street", "latitude": 12.34, "longitude": 5}, ctx
        )
    result = await tool.handler({"latitude": 48.8584, "longitude": 2.2945}, ctx)
    assert result["drive_minutes"] == 19


def test_no_home_means_no_tool():
    assert build_travel_tools(None, home=None, timezone="Europe/Paris", margin_minutes=10) == []
