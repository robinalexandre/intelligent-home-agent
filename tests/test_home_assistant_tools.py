import json

import pytest

from intelligent_home_agent.tools import ToolError, ToolRegistry, build_home_assistant_tools


@pytest.fixture
def tools(ha, policy) -> ToolRegistry:
    return ToolRegistry(build_home_assistant_tools(ha, policy))


async def test_list_entities_hides_unexposed_and_denylisted(tools, ctx):
    result = await tools.run("list_entities", {}, ctx)
    ids = {e["entity_id"] for e in result["entities"]}
    assert ids == {"light.living_room", "light.veranda", "lock.front_door", "weather.home"}


async def test_list_entities_search_ignores_case_and_accents(tools, ctx):
    result = await tools.run("list_entities", {"search": "VERANDA"}, ctx)
    assert [e["entity_id"] for e in result["entities"]] == ["light.veranda"]


async def test_get_entity_state_refuses_denylisted(tools, ctx):
    with pytest.raises(ToolError):
        await tools.run("get_entity_state", {"entity_id": "switch.secret"}, ctx)


async def test_call_service_executes_regular_action(tools, ctx, ha_mock):
    route = ha_mock.post("/api/services/light/turn_on").respond(json=[])
    result = await tools.run(
        "call_service",
        {
            "domain": "light",
            "service": "turn_on",
            "entity_id": ["light.living_room"],
            "data": {"brightness_pct": 50},
        },
        ctx,
    )
    assert result["status"] == "done"
    assert json.loads(route.calls.last.request.content) == {
        "brightness_pct": 50,
        "entity_id": ["light.living_room"],
    }


@pytest.mark.parametrize(
    "args, message",
    [
        (
            {"domain": "camera", "service": "turn_on", "entity_id": ["camera.garden"]},
            "not available",
        ),
        (
            {"domain": "light", "service": "turn_on", "entity_id": ["lock.front_door"]},
            "is not a 'light' entity",
        ),
        (
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": ["light.living_room"],
                "data": {"entity_id": "lock.front_door"},
            },
            "Targets go in entity_id only",
        ),
        (
            {"domain": "switch", "service": "turn_on", "entity_id": ["switch.secret"]},
            "not available",
        ),
    ],
)
async def test_call_service_guardrails(tools, ctx, ha_mock, args, message):
    with pytest.raises(ToolError, match=message):
        await tools.run("call_service", args, ctx)
    assert not any(c.request.method == "POST" for c in ha_mock.calls)


async def test_sensitive_action_requires_confirmation_in_a_later_turn(tools, ctx, ha_mock):
    route = ha_mock.post("/api/services/lock/lock").respond(json=[])
    pending = await tools.run(
        "call_service",
        {"domain": "lock", "service": "lock", "entity_id": ["lock.front_door"]},
        ctx,
    )
    assert pending["status"] == "needs_confirmation"
    assert not route.called

    # Same turn: the model can't confirm on the user's behalf.
    with pytest.raises(ToolError, match="not confirmed"):
        await tools.run(
            "confirm_pending_action", {"confirmation_id": pending["confirmation_id"]}, ctx
        )
    assert not route.called

    # Next user turn: confirmation goes through, exactly once.
    ctx.conversation.turn += 1
    result = await tools.run(
        "confirm_pending_action", {"confirmation_id": pending["confirmation_id"]}, ctx
    )
    assert result["status"] == "done"
    assert route.call_count == 1
    with pytest.raises(ToolError, match="No pending action"):
        await tools.run(
            "confirm_pending_action", {"confirmation_id": pending["confirmation_id"]}, ctx
        )


async def test_weather_forecast_uses_first_weather_entity(tools, ctx, ha_mock):
    forecast = [{"datetime": f"2026-10-1{i}", "condition": "sunny"} for i in range(7)]
    ha_mock.post("/api/services/weather/get_forecasts", params={"return_response": "true"}).respond(
        json={"service_response": {"weather.home": {"forecast": forecast}}}
    )
    result = await tools.run("get_weather_forecast", {}, ctx)
    assert result["entity_id"] == "weather.home"
    assert len(result["forecast"]) == 5
