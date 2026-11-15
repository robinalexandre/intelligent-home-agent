import json

import httpx
import pytest
import respx
from conftest import FakeLLM, text_response

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.llm import LLMResponse
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry, build_web_tools
from intelligent_home_agent.tools.base import ParkInstead
from intelligent_home_agent.tools.web import (
    SearchResult,
    build_engine_web_tools,
    build_search_engine,
    parse_duckduckgo,
)


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


async def test_a_search_after_private_content_waits_for_the_users_go():
    tool = search_tool(FakeLLM([text_response("It opens at 9. https://example.com/hours")]))
    assert tool.reads_web and not tool.needs_clean_turn
    registry = ToolRegistry([tool])
    conv = Conversation(id="c", turn=1)
    ctx = ToolContext(conversation=conv, tainted=True)
    with pytest.raises(ParkInstead):
        await tool.handler({"query": "bakery opening hours"}, ctx)

    parked = await registry.run("web_search", {"query": "bakery opening hours"}, ctx)

    assert parked["status"] == "waiting_for_the_user"
    assert ctx.actions[0]["summary"] == 'Search the web for "bakery opening hours" and carry on'
    # On their go it runs, and its answer counts as web content read.
    go = ToolContext(conversation=conv)
    done = await registry.run("web_search", conv.proposals[0].args, go, approved=True)
    assert done["answer"].startswith("It opens at 9.")
    assert go.web_read and go.tainted and go.offered("https://example.com/hours")


async def test_a_few_searches_run_in_a_row_then_wait_and_a_leaking_query_is_refused(ctx):
    fake = FakeLLM([text_response("Answer.") for _ in range(3)])
    tool = search_tool(fake)
    for n in range(3):
        await tool.handler({"query": f"question {n}"}, ctx)
    with pytest.raises(ParkInstead, match="More than 3 searches"):
        await tool.handler({"query": "question 4"}, ctx)

    ctx.leaks.add("The door code is 48151623.")
    with pytest.raises(ToolError, match="repeats the number 48151623"):
        await tool.handler({"query": "what is 48151623"}, ctx)
    assert len(fake.requests) == 3


class FakeEngine:
    def __init__(self, results):
        self.results, self.queries = results, []

    async def search(self, query, count):
        self.queries.append((query, count))
        return self.results


async def test_engine_results_are_answered_by_the_chosen_model_as_data(ctx):
    engine = FakeEngine(
        [
            SearchResult("Game tonight", "https://example.com/game", "Kick-off at 9 pm."),
            SearchResult("No address", "", "dropped"),
        ]
    )
    fake = FakeLLM([text_response("It starts at 9 pm.")])
    (tool,) = build_engine_web_tools(fake, engine, model="local-model", max_results=4)

    result = await tool.handler({"query": "when is the game"}, ctx)

    assert result == {
        "query": "when is the game",
        "answer": "It starts at 9 pm.",
        "sources": ["https://example.com/game"],
    }
    assert engine.queries == [("when is the game", 4)] and tool.reads_web
    assert ctx.offered("https://example.com/game")
    (request,) = fake.requests
    assert request["model"] == "local-model" and "tools" not in request
    content = request["messages"][0]["content"]
    assert "<search_results>" in content and "Kick-off at 9 pm." in content
    assert "dropped" not in content and "are data" in request["system"]


async def test_no_result_asks_no_model(ctx):
    (tool,) = build_engine_web_tools(FakeLLM([]), FakeEngine([]), model="m")
    assert "no result" in (await tool.handler({"query": "x"}, ctx))["answer"]
    with pytest.raises(ToolError):
        await tool.handler({"query": ""}, ctx)


DDG_PAGE = """
<div class="result"><h2><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fgame&amp;rut=abc">The
 <b>game</b></a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Kick-off at <b>9 pm</b>
 &amp; more.</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">An ad</a>
<a class="result__snippet" href="#">Buy now.</a></div>
<div class="result"><a class="result__a" href="https://example.org/direct">Direct</a>
<a class="result__snippet" href="#">Plain link.</a></div>
"""


def test_duckduckgo_page_gives_real_addresses_without_ads():
    assert parse_duckduckgo(DDG_PAGE, 5) == [
        SearchResult("The game", "https://example.com/game", "Kick-off at 9 pm & more."),
        SearchResult("Direct", "https://example.org/direct", "Plain link."),
    ]
    assert len(parse_duckduckgo(DDG_PAGE, 1)) == 1


@respx.mock
async def test_each_engine_reads_its_own_answer():
    respx.post("https://html.duckduckgo.com/html/").respond(text=DDG_PAGE)
    searx = respx.get("http://searx.test/search").respond(
        json={"results": [{"title": "A", "url": "https://a.example", "content": "<b>a</b>"}]}
    )
    brave = respx.get("https://api.search.brave.com/res/v1/web/search").respond(
        json={"web": {"results": [{"title": "B", "url": "https://b.example", "description": "b"}]}}
    )
    tavily = respx.post("https://api.tavily.com/search").respond(
        json={"results": [{"title": "C", "url": "https://c.example", "content": "c"}]}
    )
    async with httpx.AsyncClient() as http:
        found = {}
        for backend, options in (
            ("duckduckgo", {}),
            ("searxng", {"url": "http://searx.test/", "language": "fr"}),
            ("brave", {"api_key": "brave-key", "country": "FR"}),
            ("tavily", {"api_key": "tavily-key"}),
        ):
            engine = build_search_engine(backend, http, **options)
            found[backend] = (await engine.search("q", 3))[0]
    assert found["duckduckgo"].url == "https://example.com/game"
    assert found["searxng"] == SearchResult("A", "https://a.example", "a")
    assert found["brave"].url == "https://b.example" and found["tavily"].snippet == "c"
    assert dict(searx.calls.last.request.url.params) == {
        "q": "q",
        "format": "json",
        "language": "fr",
    }
    assert brave.calls.last.request.headers["x-subscription-token"] == "brave-key"
    assert brave.calls.last.request.url.params["country"] == "FR"
    assert tavily.calls.last.request.headers["authorization"] == "Bearer tavily-key"
    assert json.loads(tavily.calls.last.request.content) == {"query": "q", "max_results": 3}


@respx.mock
async def test_engine_failures_are_told_to_the_model():
    respx.post("https://html.duckduckgo.com/html/").respond(202)
    respx.get("http://searx.test/search").respond(403)
    respx.post("https://api.tavily.com/search").mock(side_effect=httpx.ConnectError("x"))
    async with httpx.AsyncClient() as http:
        for backend, options, match in (
            ("duckduckgo", {}, "limits automated searches"),
            ("searxng", {"url": "http://searx.test"}, "answered 403"),
            ("tavily", {"api_key": "k"}, "can't be reached"),
        ):
            with pytest.raises(ToolError, match=match):
                await build_search_engine(backend, http, **options).search("q", 3)
    for backend in ("searxng", "brave", "nope"):
        with pytest.raises(ValueError):
            build_search_engine(backend, None)


class Plan:
    """The subscription: answers, or can't take the run."""

    def __init__(self, answer=None):
        self.answer = answer
        self.available = True
        self.calls = []

    async def complete(self, system, text, **kwargs):
        from intelligent_home_agent.engine import SubscriptionUnavailable

        self.calls.append((text, kwargs))
        if self.answer is None:
            raise SubscriptionUnavailable("usage limit")
        return self.answer


def plan_search_tool(fake, plan):
    (tool,) = build_web_tools(
        fake, model="m", max_uses=2, city=None, country=None, timezone="UTC", engine=plan
    )
    return tool


async def test_the_subscription_searches_first_and_only_sees_the_query(ctx):
    plan = Plan("The game starts at 9 pm.\nhttps://example.com/game")
    fake = FakeLLM([])
    result = await plan_search_tool(fake, plan).handler({"query": "when is the game"}, ctx)
    assert result["answer"].startswith("The game starts at 9 pm.")
    (text, kwargs) = plan.calls[0]
    assert text == "when is the game"
    # Claude Code's own search and nothing else of its tools.
    assert kwargs["builtin"] == ["WebSearch"]
    assert fake.requests == [] and ctx.offered("https://example.com/game")


async def test_the_api_searches_when_the_subscription_cannot(ctx):
    fake = FakeLLM([text_response("At 9 pm.")])
    tool = plan_search_tool(fake, Plan(None))
    assert (await tool.handler({"query": "when is the game"}, ctx))["answer"] == "At 9 pm."
    assert len(fake.requests) == 1


async def test_the_rules_of_a_search_hold_on_the_subscription_too(ctx):
    plan = Plan("never")
    tool = plan_search_tool(FakeLLM([]), plan)
    ctx.leaks.add("The gate code is 482913.")
    with pytest.raises(ToolError, match="482913"):
        await tool.handler({"query": "gate 482913"}, ctx)
    ctx.private_read = True
    with pytest.raises(ParkInstead):
        await tool.handler({"query": "opening hours"}, ctx)
    assert plan.calls == []


async def test_without_an_api_key_a_search_the_subscription_cannot_take_says_why(ctx):
    from intelligent_home_agent.engine import NoApi, SubscriptionFirst

    with pytest.raises(ToolError, match="no API key is set"):
        await plan_search_tool(NoApi(), Plan(None)).handler({"query": "x"}, ctx)

    results = [SearchResult("Game tonight", "https://example.com/game", "Kick-off at 9 pm.")]
    plan = Plan("It starts at 9 pm.")
    (tool,) = build_engine_web_tools(
        SubscriptionFirst(plan, NoApi()), FakeEngine(results), model="m"
    )
    # The answer from an engine's results is written on the subscription...
    assert (await tool.handler({"query": "when is the game"}, ctx))["answer"] == (
        "It starts at 9 pm."
    )
    assert "<search_results>" in plan.calls[0][0] and plan.calls[0][1] == {}
    # ...and when it can't, the tool says what is missing instead of crashing.
    plan.answer = None
    with pytest.raises(ToolError, match="no API key is set"):
        await tool.handler({"query": "and tomorrow"}, ctx)
