"""The subscription engine, with the Agent SDK replaced by a scripted stand-in."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from intelligent_home_agent.engine import (
    FAILURE_PAUSE_SECONDS,
    LIMIT_PAUSE_SECONDS,
    BuiltinGuard,
    ClaudeCodeEngine,
    SubscriptionUnavailable,
    render_prompt,
    subscription_first,
)
from intelligent_home_agent.tools import ToolRegistry


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict[str, Any]


@dataclass
class AssistantMessage:
    content: list[Any]


@dataclass
class UserMessage:
    content: Any


@dataclass
class ResultMessage:
    is_error: bool = False
    result: str | None = None
    subtype: str = "success"


def init(source: str = "none") -> SystemMessage:
    return SystemMessage("init", {"apiKeySource": source, "session_id": "s1"})


class Call:
    """A step of the script: Claude Code calls one of the MCP tools."""

    def __init__(self, tool_use_id: str, name: str, args: dict[str, Any]) -> None:
        self.id, self.name, self.args = tool_use_id, name, args


class FakeSdk:
    """What the engine uses of claude_agent_sdk: the message classes, the tool decorator,
    the server factory and a client that plays a script, running the MCP tools it names."""

    TextBlock, ToolUseBlock, ToolResultBlock = TextBlock, ToolUseBlock, ToolResultBlock
    HookMatcher = staticmethod(
        lambda matcher=None, hooks=None: SimpleNamespace(matcher=matcher, hooks=hooks)
    )
    SystemMessage, AssistantMessage, UserMessage = SystemMessage, AssistantMessage, UserMessage
    ResultMessage = ResultMessage

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.options: Any = None
        self.prompts: list[str] = []
        self.ClaudeAgentOptions = lambda **kwargs: SimpleNamespace(**kwargs)
        self.ClaudeSDKClient = self._client

    @staticmethod
    def tool(name: str, description: str, input_schema: Any):
        def decorate(handler):
            return SimpleNamespace(
                name=name, description=description, handler=handler, input_schema=input_schema
            )

        return decorate

    @staticmethod
    def create_sdk_mcp_server(name: str, version: str = "1.0.0", tools: list | None = None):
        return {"name": name, "tools": {t.name: t for t in tools or []}}

    def _client(self, options: Any) -> FakeSdk:
        self.options = options
        return self

    async def __aenter__(self) -> FakeSdk:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self):
        tools = self.options.mcp_servers.get("agent", {}).get("tools", {})
        for step in self.script:
            if isinstance(step, Call):
                yield AssistantMessage(
                    [ToolUseBlock(step.id, f"mcp__agent__{step.name}", step.args)]
                )
                result = await tools[step.name].handler(step.args)
                yield UserMessage(
                    [ToolResultBlock(step.id, result["content"], result.get("is_error"))]
                )
            else:
                yield step


def registry() -> ToolRegistry:
    from intelligent_home_agent.tools import Tool

    async def lights(_args, _ctx):
        return {"on": ["living room"]}

    return ToolRegistry([Tool("lights", "Which lights are on.", {"type": "object"}, lights)])


CONFIG_DIR = Path(tempfile.mkdtemp()) / "claude"


def engine(sdk: FakeSdk, clock=None) -> ClaudeCodeEngine:
    return ClaudeCodeEngine(
        model="claude-test-opus",
        token="sk-ant-oat01-test",
        config_dir=CONFIG_DIR,
        sdk=sdk,
        **({"clock": clock} if clock else {}),
    )


async def test_a_turn_runs_the_tools_through_the_bridge_and_comes_back_as_api_messages():
    sdk = FakeSdk(
        [
            init(),
            AssistantMessage([TextBlock("Let me look.")]),
            Call("t1", "lights", {"room": "living room"}),
            AssistantMessage([TextBlock("The living room is on.")]),
            ResultMessage(result="The living room is on."),
        ]
    )
    calls: list[tuple[str, dict]] = []
    updates: list[str] = []

    async def call(name: str, args: dict) -> tuple[Any, bool]:
        calls.append((name, args))
        return '{"on": ["living room"]}', False

    async def progress(text: str) -> None:
        updates.append(text)

    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "Hi Sam."}]},
        {"role": "user", "content": "Which lights are on?"},
    ]
    turn = await engine(sdk).run_turn(
        "You are Nova.", history, registry(), call, max_turns=6, progress=progress
    )

    assert turn.text == "The living room is on."
    assert calls == [("lights", {"room": "living room"})]
    assert turn.messages == [
        {"role": "assistant", "content": [{"type": "text", "text": "Let me look."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "lights", "input": {"room": "living room"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": '{"on": ["living room"]}'}],
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "The living room is on."}]},
    ]
    assert updates == []  # the text before the call was in a message of its own
    options = sdk.options
    assert options.model == "claude-test-opus"
    # The prompt names the tools bare; Claude Code only knows them with the server's prefix.
    assert options.system_prompt.startswith("You are Nova.\n\nHere your tools are named")
    assert "get_weather is mcp__agent__get_weather" in options.system_prompt
    assert options.tools == [] and options.setting_sources == []
    assert options.allowed_tools == ["mcp__agent__lights"]
    # A tool without arguments still says so: no "type" argument for the model to invent.
    bridged = options.mcp_servers["agent"]["tools"]["lights"]
    assert bridged.input_schema == {"type": "object", "properties": {}}
    assert options.permission_mode == "dontAsk" and options.max_turns == 6
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert options.env["ANTHROPIC_API_KEY"] == "" and options.env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert options.env["CLAUDE_CONFIG_DIR"] == str(CONFIG_DIR) == options.cwd
    assert CONFIG_DIR.is_dir()
    assert sdk.prompts == [
        "<conversation_so_far>\nUser: Hello\n\nAssistant: Hi Sam.\n</conversation_so_far>"
        "\n\nThe user's new message:\nWhich lights are on?"
    ]


async def test_text_with_a_tool_call_is_a_progress_update_and_errors_pass_through():
    sdk = FakeSdk(
        [
            init(),
            AssistantMessage(
                [TextBlock("Checking."), ToolUseBlock("t1", "mcp__agent__lights", {})]
            ),
            UserMessage([ToolResultBlock("t1", "boom", True)]),
            ResultMessage(result=""),
        ]
    )
    updates: list[str] = []

    async def progress(text: str) -> None:
        updates.append(text)

    async def call(name, args):
        raise AssertionError("not reached: the script answers itself")

    turn = await engine(sdk).run_turn(
        "sys",
        [{"role": "user", "content": "Lights?"}],
        registry(),
        call,
        max_turns=3,
        progress=progress,
    )
    assert updates == ["Checking."]
    assert turn.messages[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "boom",
        "is_error": True,
    }
    # The turn ends on an assistant text, so the history stays valid for the API.
    assert turn.messages[-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "Checking."}],
    }
    assert turn.text == "Checking."


async def test_a_tool_result_with_a_file_becomes_a_note_and_errors_are_flagged():
    sdk = FakeSdk([init(), Call("t1", "lights", {}), ResultMessage(result="Ok.")])

    async def call(name, args):
        return [
            {"type": "document", "source": {}},
            {"type": "text", "text": "Kept as a.pdf."},
        ], True

    turn = await engine(sdk).run_turn(
        "sys", [{"role": "user", "content": "Read the PDF"}], registry(), call, max_turns=3
    )
    result = turn.messages[1]["content"][0]
    assert result["is_error"] is True
    assert [b["text"] for b in result["content"]] == [
        "[a file was read and kept for the user; its content is not readable here]",
        "Kept as a.pdf.",
    ]
    assert turn.text == "Ok."


async def test_a_run_that_would_bill_an_api_key_disables_the_engine():
    sdk = FakeSdk([init("ANTHROPIC_API_KEY"), ResultMessage(result="hi")])
    eng = engine(sdk)

    async def call(name, args):
        return "", False

    with pytest.raises(SubscriptionUnavailable, match="bill an API key"):
        await eng.run_turn(
            "sys", [{"role": "user", "content": "Hi"}], registry(), call, max_turns=3
        )
    assert not eng.available


async def test_a_completion_answers_one_prompt_without_tools():
    sdk = FakeSdk([init(), AssistantMessage([TextBlock("[]")]), ResultMessage(result="[]")])
    assert await engine(sdk).complete("You curate notes.", "The lessons.") == "[]"
    options = sdk.options
    assert options.system_prompt == "You curate notes." and sdk.prompts == ["The lessons."]
    assert options.tools == [] and options.mcp_servers == {} and options.allowed_tools == []
    assert options.max_turns == 1 and options.env["ANTHROPIC_API_KEY"] == ""

    billed = engine(FakeSdk([init("ANTHROPIC_API_KEY")]))
    with pytest.raises(SubscriptionUnavailable, match="bill an API key"):
        await billed.complete("sys", "x")
    with pytest.raises(SubscriptionUnavailable, match="no answer"):
        await engine(FakeSdk([init(), ResultMessage(result="")])).complete("sys", "x")


def files_in(folder: str) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in Path(folder).iterdir()}


def gone(folder: str) -> bool:
    return not Path(folder).exists()


async def test_a_completion_may_use_builtin_tools_on_files_it_reads(tmp_path):
    seen: dict[str, bytes] = {}

    class Reading(FakeSdk):
        async def query(self, prompt):
            await super().query(prompt)
            seen.update(files_in(self.options.cwd))

    sdk = Reading([init(), AssistantMessage([TextBlock("text")]), ResultMessage(result="text")])
    eng = ClaudeCodeEngine(model="m", token="t", config_dir=tmp_path, sdk=sdk)
    answer = await eng.complete(
        "", "Read it.", builtin=["Read"], files={"scan.pdf": b"%PDF"}, max_turns=4
    )
    assert answer == "text"
    assert seen == {"scan.pdf": b"%PDF"}
    assert sdk.options.tools == ["Read"] and sdk.options.allowed_tools == ["Read"]
    assert sdk.options.max_turns == 4
    assert "scan.pdf" in sdk.prompts[0]
    # The run's folder is gone once it ended.
    assert gone(sdk.options.cwd)


async def test_the_guard_keeps_reads_to_the_run_and_fetches_to_known_addresses(tmp_path):
    folder = tmp_path / "run-1"
    folder.mkdir()
    guard = BuiltinGuard(folder, {"https://shop.example"}, max_fetches=2)
    assert guard.refusal("Read", {"file_path": str(folder / "scan.pdf")}) is None
    assert guard.refusal("Read", {"file_path": str(folder / ".." / "tokens.json")})
    assert guard.refusal("Read", {"file_path": "/app/data/claude/.credentials.json"})
    assert BuiltinGuard(None, set()).refusal("Read", {"file_path": str(folder / "x")})
    assert guard.refusal("WebFetch", {"url": "https://evil.example/?q=secret"})
    assert guard.refusal("WebFetch", {"url": "https://shop.example/"}) is None
    guard.learn("WebSearch", {"results": [{"url": "https://news.example/shop"}]})
    assert guard.refusal("WebFetch", {"url": "https://news.example/shop"}) is None
    # An address the query carried, echoed back in the response, is not learnt; nor text.
    leak = "https://evil.example/?q=data"
    guard.learn(
        "WebSearch",
        {"query": leak, "results": [{"url": leak}], "text": "see https://other.example/x"},
        query=f"shop {leak}",
    )
    assert guard.refusal("WebFetch", {"url": leak})
    assert guard.refusal("WebFetch", {"url": "https://other.example/x"})
    assert "No more pages" in guard.refusal("WebFetch", {"url": "https://shop.example"})


async def test_a_run_with_builtin_tools_carries_the_guards_hooks(tmp_path):
    sdk = FakeSdk([init(), ResultMessage(result="ok")])
    eng = ClaudeCodeEngine(model="m", token="t", config_dir=tmp_path, sdk=sdk)
    await eng.complete("", "q", builtin=["WebSearch"], max_turns=3)
    before = sdk.options.hooks["PreToolUse"][0]
    assert before.matcher == "Read|WebFetch"
    denied = await before.hooks[0](
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}}, None, None
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_subscription_first_falls_back_to_the_api():
    async def refused(engine):
        raise SubscriptionUnavailable("usage limit")

    async def on_subscription(engine):
        return "plan"

    async def on_api():
        return "api"

    eng = engine(FakeSdk([]))
    assert await subscription_first(eng, on_subscription, on_api) == "plan"
    assert await subscription_first(eng, refused, on_api) == "api"
    assert await subscription_first(None, on_subscription, on_api) == "api"


async def test_a_turn_out_of_rounds_ends_without_failing_or_pausing_the_engine():
    sdk = FakeSdk(
        [
            init(),
            Call("t1", "lights", {}),
            ResultMessage(is_error=True, subtype="error_max_turns"),
        ]
    )
    eng = engine(sdk)

    async def call(name, args):
        return "on", False

    turn = await eng.run_turn(
        "sys", [{"role": "user", "content": "Everything"}], registry(), call, max_turns=1
    )

    assert turn.out_of_rounds and turn.messages[0]["content"][0]["name"] == "lights"
    assert eng.available


async def test_a_usage_limit_pauses_the_engine_longer_than_another_error():
    now = [1000.0]
    limited = FakeSdk(
        [init(), ResultMessage(is_error=True, result="Claude AI usage limit reached|1761300000")]
    )
    eng = engine(limited, clock=lambda: now[0])

    async def call(name, args):
        return "", False

    with pytest.raises(SubscriptionUnavailable, match="usage limit"):
        await eng.run_turn("sys", [{"role": "user", "content": "x"}], registry(), call, max_turns=3)
    assert not eng.available
    now[0] += LIMIT_PAUSE_SECONDS - 1
    assert not eng.available
    now[0] += 1
    assert eng.available

    class Crashing(FakeSdk):
        async def query(self, prompt):
            raise RuntimeError("no claude binary")

    eng = engine(Crashing([init()]), clock=lambda: now[0])
    with pytest.raises(SubscriptionUnavailable, match="no claude binary"):
        await eng.run_turn("sys", [{"role": "user", "content": "x"}], registry(), call, max_turns=3)
    now[0] += FAILURE_PAUSE_SECONDS
    assert eng.available
    with pytest.raises(SubscriptionUnavailable, match="paused"):
        now[0] -= 1
        await eng.run_turn("sys", [{"role": "user", "content": "x"}], registry(), call, max_turns=3)


async def test_a_message_with_a_file_goes_to_the_api_before_anything_starts():
    sdk = FakeSdk([init()])

    async def call(name, args):
        return "", False

    messages = [
        {
            "role": "user",
            "content": [{"type": "document", "source": {}}, {"type": "text", "text": "Sum it up"}],
        }
    ]
    with pytest.raises(SubscriptionUnavailable, match="file"):
        await engine(sdk).run_turn("sys", messages, registry(), call, max_turns=3)
    assert sdk.prompts == [] and sdk.options is None


def test_the_prompt_renders_tool_calls_results_and_old_files_in_the_history():
    messages = [
        {
            "role": "user",
            "content": [{"type": "image", "source": {}}, {"type": "text", "text": "And this?"}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "lights",
                    "input": {"room": "living room"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "on:   living room"}],
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "The living room."}]},
        {"role": "user", "content": "Thanks"},
    ]
    assert render_prompt(messages) == (
        "<conversation_so_far>\n"
        "User: [a file]\nAnd this?\n\n"
        'Assistant: Let me look.\n[called mcp__agent__lights with {"room": "living room"}]\n\n'
        "User: [result: on: living room]\n\n"
        "Assistant: The living room.\n"
        "</conversation_so_far>\n\nThe user's new message:\nThanks"
    )
    assert render_prompt([{"role": "user", "content": "Alone"}]) == "Alone"


def test_a_schema_without_properties_asks_for_no_argument_on_the_real_sdk():
    claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
    from intelligent_home_agent.engine import wire_schema

    async def handler(_args: Any) -> dict[str, Any]:
        return {"content": []}

    tool = claude_agent_sdk.tool("get_status", "The status.", wire_schema({"type": "object"}))(
        handler
    )
    built = claude_agent_sdk._build_input_schema(tool)
    assert built == {"type": "object", "properties": {}}
    kept = wire_schema({"type": "object", "properties": {"id": {"type": "string"}}})
    assert kept == {"type": "object", "properties": {"id": {"type": "string"}}}


def test_a_picture_in_a_tool_result_reaches_the_engine_as_a_picture():
    from intelligent_home_agent.engine import FILE_NOTE, _mcp_content

    blocks = _mcp_content(
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ"},
            },
            {"type": "document", "source": {"type": "base64", "data": "QQ"}},
            {"type": "text", "text": "ok"},
        ]
    )
    assert blocks == [
        {"type": "image", "data": "QQ", "mimeType": "image/jpeg"},
        {"type": "text", "text": FILE_NOTE},
        {"type": "text", "text": "ok"},
    ]


class Plan:
    """An engine that answers a plain prompt, or can't take it."""

    def __init__(self, answer: str | None) -> None:
        self.answer, self.available, self.asked = answer, True, []

    async def complete(self, system: str, text: str, **kwargs: Any) -> str:
        self.asked.append((system, text, kwargs))
        if self.answer is None:
            raise SubscriptionUnavailable("usage limit")
        return self.answer


async def test_a_plain_model_call_goes_to_the_plan_first_and_anything_else_to_the_api():
    from conftest import FakeLLM, text_response

    from intelligent_home_agent.engine import SubscriptionFirst

    plain = dict(
        model="m",
        max_tokens=100,
        system=[{"type": "text", "text": "Answer from the results."}],
        messages=[{"role": "user", "content": "Question: opening hours"}],
    )
    plan, api = Plan("At 9."), FakeLLM([text_response("api")])
    llm = SubscriptionFirst(plan, api)
    response = await llm.create(**plain)
    assert response.stop_reason == "end_turn"
    assert response.content == [{"type": "text", "text": "At 9."}]
    assert plan.asked == [("Answer from the results.", "Question: opening hours", {})]
    assert api.requests == []

    # Tools, a file or a conversation: only the API takes those.
    with_file = [{"type": "document", "source": {}}, {"type": "text", "text": "Read it."}]
    for request in (
        {**plain, "tools": [{"name": "web_search"}]},
        {**plain, "messages": [{"role": "user", "content": with_file}]},
        {**plain, "messages": [*plain["messages"], {"role": "assistant", "content": "x"}] * 2},
    ):
        api = FakeLLM([text_response("api")])
        answer = await SubscriptionFirst(plan, api).create(**request)
        assert answer.content[0]["text"] == "api" and len(api.requests) == 1
    assert len(plan.asked) == 1

    # A limit, a pause, or no engine at all: the API.
    for engine in (Plan(None), None):
        api = FakeLLM([text_response("api")])
        assert (await SubscriptionFirst(engine, api).create(**plain)).content[0]["text"] == "api"


async def test_without_an_api_key_what_the_plan_cannot_take_fails_with_the_reason():
    from intelligent_home_agent.engine import NoApi, NoApiKey, SubscriptionFirst
    from intelligent_home_agent.llm import LLMError

    with pytest.raises(NoApiKey, match="no API key is set") as caught:
        await SubscriptionFirst(Plan(None), NoApi()).create(
            model="m", messages=[{"role": "user", "content": "x"}]
        )
    assert isinstance(caught.value, LLMError)
    # Nothing of the API client is there to mistake for one (the code sandbox's files).
    assert getattr(NoApi(), "files", None) is None
    assert getattr(SubscriptionFirst(None, NoApi()), "files", None) is None


def test_the_options_the_engine_sets_exist_on_the_real_sdk(tmp_path):
    sdk = pytest.importorskip("claude_agent_sdk")
    guard = BuiltinGuard(tmp_path, set())
    options = sdk.ClaudeAgentOptions(
        model="m",
        system_prompt="s",
        tools=[],
        mcp_servers={},
        allowed_tools=[],
        permission_mode="dontAsk",
        setting_sources=[],
        max_turns=1,
        cwd=str(tmp_path),
        hooks=guard.hooks(sdk),
        env={"CLAUDE_CODE_OAUTH_TOKEN": "t"},
    )
    assert options.tools == [] and options.setting_sources == []
    for name in ("ClaudeSDKClient", "SystemMessage", "AssistantMessage", "UserMessage"):
        assert hasattr(sdk, name)
    for name in ("ResultMessage", "TextBlock", "ToolUseBlock", "ToolResultBlock", "tool"):
        assert hasattr(sdk, name)
    assert hasattr(sdk, "create_sdk_mcp_server")
