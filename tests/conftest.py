from __future__ import annotations

import copy
from typing import Any

import pytest
import respx

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.ha_client import HomeAssistantClient
from intelligent_home_agent.llm import LLMResponse
from intelligent_home_agent.tools import EntityPolicy, ToolContext

HA_URL = "http://ha.test"

STATES = [
    {
        "entity_id": "light.living_room",
        "state": "off",
        "attributes": {"friendly_name": "Living room lamp"},
    },
    {
        "entity_id": "light.veranda",
        "state": "on",
        "attributes": {"friendly_name": "Véranda light", "brightness": 200},
    },
    {
        "entity_id": "lock.front_door",
        "state": "unlocked",
        "attributes": {"friendly_name": "Front door"},
    },
    {
        "entity_id": "switch.secret",
        "state": "off",
        "attributes": {"friendly_name": "Secret switch"},
    },
    {
        "entity_id": "camera.garden",
        "state": "idle",
        "attributes": {"friendly_name": "Garden camera"},
    },
    {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Weather"}},
]


@pytest.fixture
def ha_mock():
    with respx.mock(base_url=HA_URL, assert_all_called=False) as mock:
        mock.get("/api/states").respond(json=STATES)
        for state in STATES:
            mock.get(f"/api/states/{state['entity_id']}").respond(json=state)
        mock.get("/api/").respond(json={"message": "API running."})
        yield mock


@pytest.fixture
async def ha(ha_mock):
    client = HomeAssistantClient(HA_URL, "test-token")
    yield client
    await client.aclose()


@pytest.fixture
def policy() -> EntityPolicy:
    return EntityPolicy.from_lists(
        exposed=["light", "switch", "lock", "weather"],
        sensitive=["lock"],
        denylist=["switch.secret"],
    )


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(conversation=Conversation(id="test", turn=1))


def text_response(text: str) -> LLMResponse:
    return LLMResponse("end_turn", [{"type": "text", "text": text}])


def tool_response(*calls: tuple[str, str, dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        "tool_use",
        [{"type": "tool_use", "id": cid, "name": name, "input": args} for cid, name, args in calls],
    )


class FakeLLM:
    """Returns scripted responses and records every request."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> LLMResponse:
        # Snapshot messages: the agent mutates them afterwards.
        self.requests.append({**kwargs, "messages": copy.deepcopy(kwargs["messages"])})
        return self._responses.pop(0)
