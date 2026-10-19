import pytest
from conftest import FakeLLM

from intelligent_home_agent.llm import LLMResponse
from intelligent_home_agent.tools import ToolError, build_web_tools


def search_tool(fake):
    (tool,) = build_web_tools(
        fake,
        model="search-model",
        max_uses=2,
        city="Springfield",
        country="US",
        timezone="Europe/Paris",
    )
    return tool


async def test_search_runs_in_its_own_request_and_resumes_pauses(ctx):
    paused = [
        {"type": "text", "text": "Searching."},
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "game"}},
    ]
    done = [
        {"type": "text", "text": "The game starts "},
        {"type": "text", "text": "at 9 pm."},
    ]
    fake = FakeLLM([LLMResponse("pause_turn", paused), LLMResponse("end_turn", done)])

    result = await search_tool(fake).handler({"query": "when is the game tonight"}, ctx)

    assert result["answer"] == "The game starts at 9 pm."
    first, second = fake.requests
    assert first["messages"] == [{"role": "user", "content": "when is the game tonight"}]
    assert first["model"] == "search-model"
    assert first["tools"][0]["user_location"]["city"] == "Springfield"
    assert second["messages"][-1]["role"] == "assistant"


async def test_empty_query_is_refused(ctx):
    with pytest.raises(ToolError):
        await search_tool(FakeLLM([])).handler({"query": " "}, ctx)


def test_search_is_blocked_once_the_request_read_private_data():
    assert search_tool(FakeLLM([])).needs_clean_turn
