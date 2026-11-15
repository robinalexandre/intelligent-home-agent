from __future__ import annotations

import json

import httpx
import pytest
import respx

from intelligent_home_agent import main
from intelligent_home_agent.llm import (
    AnthropicClient,
    LLMError,
    OpenAICompatibleClient,
    build_llm_client,
    from_chat_choice,
    to_chat_messages,
)

BASE = "http://model.test/v1"
TOOLS = [
    {
        "name": "get_weather",
        "description": "Weather in a city.",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
    }
]
# One full turn in the Messages format: a question, a tool call, its result.
HISTORY = [
    {"role": "user", "content": "Weather in Paris?"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
    },
    {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": '{"temp": 18}'}],
    },
]


def test_history_becomes_chat_messages():
    assert to_chat_messages("Be brief.", HISTORY) == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Weather in Paris?"},
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 18}'},
    ]


def test_a_failed_tool_result_says_so():
    failed = {
        "type": "tool_result",
        "tool_use_id": "c",
        "content": "No such city.",
        "is_error": True,
    }
    assert to_chat_messages(None, [{"role": "user", "content": [failed]}]) == [
        {"role": "tool", "tool_call_id": "c", "content": "Error: No such city."}
    ]


def test_tool_calls_come_back_as_tool_use_even_when_the_server_says_stop():
    call = {"function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
    response = from_chat_choice(
        {"finish_reason": "stop", "message": {"content": None, "tool_calls": [call]}}
    )
    assert response.stop_reason == "tool_use"
    assert response.content == [
        {"type": "tool_use", "id": "call_0", "name": "get_weather", "input": {"city": "Paris"}}
    ]


def test_arguments_that_are_not_json_become_no_arguments():
    call = {"id": "c", "function": {"name": "get_weather", "arguments": "{city:"}}
    response = from_chat_choice({"message": {"tool_calls": [call]}})
    assert response.content[0]["input"] == {}


def test_a_cut_answer_is_reported_as_max_tokens():
    response = from_chat_choice({"finish_reason": "length", "message": {"content": "It is"}})
    assert (response.stop_reason, response.content) == (
        "max_tokens",
        [{"type": "text", "text": "It is"}],
    )


@respx.mock
async def test_a_request_round_trip():
    route = respx.post(f"{BASE}/chat/completions").respond(
        json={"choices": [{"finish_reason": "stop", "message": {"content": "18 degrees."}}]}
    )
    client = OpenAICompatibleClient(BASE, "sk-test")
    response = await client.create(
        model="some-model", max_tokens=100, system="Be brief.", tools=TOOLS, messages=HISTORY
    )
    assert (response.stop_reason, response.content[0]["text"]) == ("end_turn", "18 degrees.")
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "some-model" and body["max_tokens"] == 100
    assert body["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]


@respx.mock
async def test_a_local_server_gets_no_key_and_openai_its_own_token_limit():
    local = respx.post("http://localhost:11434/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "ok"}}]}
    )
    openai = respx.post("https://api.openai.com/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": "ok"}}]}
    )
    ask = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
    await build_llm_client("ollama").create(**ask)
    await build_llm_client("openai", "sk-test").create(**ask)
    assert "authorization" not in local.calls.last.request.headers
    assert "tools" not in json.loads(local.calls.last.request.content)
    assert json.loads(openai.calls.last.request.content)["max_completion_tokens"] == 5


@respx.mock
async def test_errors_name_the_status_and_never_the_key():
    respx.post(f"{BASE}/chat/completions").respond(404, json={"error": "model not found"})
    client = OpenAICompatibleClient(BASE, "sk-secret")
    with pytest.raises(LLMError, match="404.*model not found") as error:
        await client.create(model="nope", max_tokens=5, messages=[])
    assert "sk-secret" not in str(error.value)
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LLMError, match="can't be reached"):
        await client.create(model="m", max_tokens=5, messages=[])
    respx.post(f"{BASE}/chat/completions").respond(json={"unexpected": True})
    with pytest.raises(LLMError, match="isn't a chat completion"):
        await client.create(model="m", max_tokens=5, messages=[])


async def test_a_server_side_tool_of_another_provider_is_refused():
    client = OpenAICompatibleClient(BASE)
    with pytest.raises(LLMError, match="web_search"):
        await client.create(
            model="m",
            max_tokens=5,
            messages=[],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
        )


def test_build_picks_the_client_and_says_what_is_missing():
    assert isinstance(build_llm_client("anthropic", "sk-test"), AnthropicClient)
    assert isinstance(build_llm_client("mistral", "sk-test"), OpenAICompatibleClient)
    assert isinstance(build_llm_client("openai-compatible", None, BASE), OpenAICompatibleClient)
    for provider, match in (
        ("anthropic", "LLM_API_KEY"),
        ("openai", "LLM_API_KEY"),
        ("openai-compatible", "LLM_BASE_URL"),
        ("skynet", "Unknown LLM_PROVIDER"),
    ):
        with pytest.raises(ValueError, match=match):
            build_llm_client(provider)


def provider_env(monkeypatch, **env):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_API_TOKEN", "secret-token")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return main.Settings(_env_file=None)


def test_settings_per_provider(monkeypatch):
    settings = provider_env(monkeypatch, LLM_API_KEY="sk-test")
    assert (settings.llm_provider, settings.llm_model) == ("anthropic", "claude-sonnet-5-5")
    settings = provider_env(monkeypatch, LLM_PROVIDER="Ollama", LLM_MODEL="some-local-model")
    assert settings.llm_provider == "ollama" and settings.llm_api_key is None
    with pytest.raises(ValueError, match="needs LLM_API_KEY"):
        provider_env(monkeypatch, LLM_PROVIDER="openai", LLM_MODEL="m")
    with pytest.raises(ValueError, match="set LLM_MODEL"):
        provider_env(monkeypatch, LLM_PROVIDER="openai", LLM_API_KEY="sk-test")
    with pytest.raises(ValueError, match="needs LLM_BASE_URL"):
        provider_env(monkeypatch, LLM_PROVIDER="openai-compatible", LLM_MODEL="m")
    with pytest.raises(ValueError, match="unknown provider"):
        provider_env(monkeypatch, LLM_PROVIDER="skynet", LLM_API_KEY="k", LLM_MODEL="m")


def test_web_search_follows_the_provider(monkeypatch):
    settings = provider_env(monkeypatch, LLM_API_KEY="sk-test")
    assert (settings.web_search_backend, settings.web_search_model) == (
        "native",
        "claude-haiku-5-5",
    )
    settings = provider_env(monkeypatch, LLM_PROVIDER="ollama", LLM_MODEL="local-model")
    assert (settings.web_search_backend, settings.web_search_model) == (
        "duckduckgo",
        "local-model",
    )
    assert "web_search" in main.build_registry(settings, None, None, None).names()
    env = {"LLM_PROVIDER": "ollama", "LLM_MODEL": "m"}
    for backend, match in (
        ("native", "own search tool"),
        ("searxng", "WEB_SEARCH_URL"),
        ("brave", "WEB_SEARCH_API_KEY"),
    ):
        with pytest.raises(ValueError, match=match):
            provider_env(monkeypatch, WEB_SEARCH_BACKEND=backend, **env)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    settings = provider_env(monkeypatch, WEB_SEARCH_BACKEND="brave", **env)
    assert "web_search" not in main.build_registry(settings, None, None, None).names()


def test_what_only_the_messages_format_knows_is_carried_as_well_as_chat_allows():
    image = {"type": "base64", "media_type": "image/png", "data": "aW1n"}
    history = [
        {
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64"}, "title": "bill.pdf"},
                {"type": "image", "source": image},
                {"type": "text", "text": "What is this?", "cache_control": {"type": "ephemeral"}},
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "look", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "c1",
                    "content": [
                        {"type": "image", "source": image},
                        {"type": "text", "text": '{"clicked": true}'},
                    ],
                },
                {"type": "text", "text": "A note."},
            ],
        },
    ]
    system = [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]

    system_message, sent, _, result, shown = to_chat_messages(system, history)

    assert system_message == {"role": "system", "content": "Be brief."}
    note, picture, question = sent["content"]
    assert note["type"] == "text" and "bill.pdf: a PDF this model can't read" in note["text"]
    assert picture == {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}}
    assert question == {"type": "text", "text": "What is this?"}
    # A tool message only holds text: the picture follows it in a user message.
    assert result == {"role": "tool", "tool_call_id": "c1", "content": '{"clicked": true}'}
    assert shown["role"] == "user" and shown["content"][1] == picture
    assert shown["content"][-1] == {"type": "text", "text": "A note."}
    assert "cache_control" not in json.dumps([system_message, sent, result, shown])


@respx.mock
async def test_out_of_rounds_asks_for_words_and_cache_markers_are_not_sent():
    route = respx.post(f"{BASE}/chat/completions").respond(
        json={"choices": [{"message": {"content": "Done."}, "finish_reason": "stop"}]}
    )
    client = OpenAICompatibleClient(BASE)
    marked = [{**TOOLS[0], "cache_control": {"type": "ephemeral"}}]

    await client.create(
        model="m", max_tokens=10, tools=marked, tool_choice={"type": "none"}, messages=HISTORY
    )

    body = json.loads(route.calls.last.request.content)
    assert body["tool_choice"] == "none"
    assert "cache_control" not in json.dumps(body)


async def test_anthropic_errors_come_out_as_one_line_without_the_key():
    import anthropic

    client = AnthropicClient("sk-test-key")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class Refusing:
        async def create(self, **kwargs):
            raise anthropic.AuthenticationError(
                "invalid x-api-key", response=httpx.Response(401, request=request), body=None
            )

    class Unreachable:
        async def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=request)

    client._messages = Refusing()
    with pytest.raises(LLMError, match="answered 401: invalid x-api-key") as caught:
        await client.create(model="m", max_tokens=5, messages=[])
    assert "sk-test-key" not in str(caught.value)
    client._messages = Unreachable()
    with pytest.raises(LLMError, match="can't be reached: APIConnectionError"):
        await client.create(model="m", max_tokens=5, messages=[])
