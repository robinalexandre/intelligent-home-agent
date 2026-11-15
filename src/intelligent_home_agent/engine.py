"""The agent on the user's Claude subscription: a turn run by Claude Code through the Agent
SDK instead of the Messages API, signed in with the token of ``claude setup-token``
(ENGINE=subscription, only with LLM_PROVIDER=anthropic; docs/subscription.md).

Claude Code is the model here and nothing more: its own tools are off (the server's own
runs may allow a few, held by a BuiltinGuard: WebSearch, WebFetch, Read), its system prompt
is the agent's, no settings file is read, and the agent's tools are exposed to it as an
in-process MCP server whose handlers run the registry, so every guardrail (taint, parked
writes, scheduled tasks) applies exactly as on the API. The conversation stays the
agent's: each turn is a fresh Claude Code run that gets the history in its prompt, so what
the agent drops from memory after a reply (emails, pages) is gone for good here too.

What the subscription can't take, the API does (intelligent_home_agent.agent): a turn with
a file to read, a usage limit (the engine pauses until it may be over), any error. Without
an API key (NoApi) the user is told so instead. A run that would bill an API key instead of
the subscription is refused and disables the engine.

The SDK is an optional dependency (the ``subscription`` extra): it is imported on first
use, and :func:`sdk_installed` says whether the feature can be there at all.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from intelligent_home_agent.llm import LLMClient, LLMError, LLMResponse

log = logging.getLogger(__name__)


def urls_in(text: str) -> list[str]:
    # Imported on use: intelligent_home_agent.tools imports this module.
    from intelligent_home_agent.tools.web_pages import urls_in as found

    return found(text)


# The name of the MCP server that carries the agent's tools into Claude Code.
SERVER = "agent"
PREFIX = f"mcp__{SERVER}__"
# Claude Code's init frame says where its API key comes from; "none" means the login.
NO_API_KEY = "none"
LIMIT_PAUSE_SECONDS = 1800
FAILURE_PAUSE_SECONDS = 300
LIMIT = re.compile(r"usage limit|rate limit|limit reached|too many requests", re.IGNORECASE)
MAX_RESULT_CHARS = 400
PROMPT = """<conversation_so_far>
{history}
</conversation_so_far>

The user's new message:
{text}"""
FILE_NOTE = "[a file was read and kept for the user; its content is not readable here]"
# The system prompt and the history name the tools as the API does; here they carry the MCP
# server's prefix, and a bare name is refused as an unknown tool.
TOOL_NAMES = (
    f"\n\nHere your tools are named {PREFIX}<name>: the instructions and the conversation "
    f"name them without that prefix (get_weather is {PREFIX}get_weather). Always call them "
    "with it; a tool name passed as an argument (propose_action's tool) goes without it."
)

# Runs one of the agent's tools: the result's content (text or blocks) and whether it failed.
ToolCall = Callable[[str, dict[str, Any]], Awaitable[tuple[Any, bool]]]
Progress = Callable[[str], Awaitable[None]]


class SubscriptionUnavailable(Exception):
    """The subscription can't take this turn; the API does."""


class FileInMessage(SubscriptionUnavailable):
    """The new message holds a file, which only the API can read."""


class NoApiKey(LLMError):
    """A run only the API can take, in a setup with the subscription alone."""

    def __init__(self) -> None:
        super().__init__(
            "This needs the Anthropic API, and no API key is set: the Claude subscription "
            "couldn't take it (a usage limit, an error, or something only the API does)."
        )


class NoApi:
    """Stands where the API client goes when ENGINE=subscription runs without an API key:
    whatever the subscription can't take fails with a sentence that says why."""

    async def create(self, **kwargs: Any) -> LLMResponse:
        raise NoApiKey


def sdk_installed() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


@dataclass
class EngineTurn:
    text: str
    # The turn as API messages (assistant tool calls, their results, the final text), so
    # the history reads the same whichever engine wrote it.
    messages: list[dict[str, Any]] = field(default_factory=list)
    # The turn used up its rounds: what ran stays done, the reply says it stopped.
    out_of_rounds: bool = False


class ToolSource(Protocol):
    def definitions(self) -> list[dict[str, Any]]: ...


def _url_fields(value: Any, depth: int = 0) -> list[str]:
    """The values of the "url" keys in a tool's structured response."""
    if depth > 8:
        return []
    if isinstance(value, dict):
        found = [str(value["url"])] if isinstance(value.get("url"), str) else []
        for item in value.values():
            found += _url_fields(item, depth + 1)
        return found
    if isinstance(value, list):
        return [url for item in value for url in _url_fields(item, depth + 1)]
    return []


@dataclass
class BuiltinGuard:
    """What Claude Code's own tools may touch in a server run, whatever the documents in
    it say: Read only the run's own files (never the server's data, tokens or transcripts),
    WebFetch only addresses already in the run (the prompt, the searches' results), as the
    API's web fetch does, and at most max_fetches of them."""

    folder: Path | None
    known: set[str]
    max_fetches: int = 20
    fetches: int = 0

    def refusal(self, tool: str, given: dict[str, Any]) -> str | None:
        if tool == "Read":
            path = Path(str(given.get("file_path") or "")).resolve()
            if self.folder is None or not path.is_relative_to(self.folder.resolve()):
                return "Only this run's own files can be read."
        elif tool == "WebFetch":
            url = str(given.get("url") or "").rstrip("/")
            if self.fetches >= self.max_fetches:
                return "No more pages can be opened in this run."
            if url not in {k.rstrip("/") for k in self.known}:
                return "Only addresses already in this run (its documents, search results)."
            self.fetches += 1
        return None

    def learn(self, tool: str, response: Any, query: str = "") -> None:
        """The results' own addresses (their url fields), never one the query itself
        carried: the model writes the query, and could slip an address with data into it."""
        if tool != "WebSearch":
            return
        for url in _url_fields(response):
            if url.startswith(("https://", "http://")) and url.rstrip("/") not in query:
                self.known.add(url)

    def hooks(self, sdk: Any) -> dict[str, Any]:
        async def before(data: dict[str, Any], _id: Any, _context: Any) -> dict[str, Any]:
            reason = self.refusal(str(data.get("tool_name") or ""), data.get("tool_input") or {})
            if reason is None:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        async def after(data: dict[str, Any], _id: Any, _context: Any) -> dict[str, Any]:
            given = data.get("tool_input") or {}
            query = str(given.get("query") or "") if isinstance(given, dict) else ""
            self.learn(str(data.get("tool_name") or ""), data.get("tool_response"), query)
            return {}

        return {
            "PreToolUse": [sdk.HookMatcher(matcher="Read|WebFetch", hooks=[before])],
            "PostToolUse": [sdk.HookMatcher(matcher="WebSearch", hooks=[after])],
        }


class ClaudeCodeEngine:
    def __init__(
        self,
        *,
        model: str,
        token: str,
        config_dir: Path,
        sdk: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._token = token
        self._config_dir = config_dir
        # The claude_agent_sdk module, given by the tests; imported on first use otherwise.
        self._sdk = sdk
        self._clock = clock
        self._paused_until = 0.0
        self._broken = False

    @property
    def available(self) -> bool:
        return not self._broken and self._clock() >= self._paused_until

    def pause(self, seconds: float, why: str) -> None:
        self._paused_until = self._clock() + seconds
        log.warning("Subscription engine paused for %d s: %s", seconds, why)

    async def run_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: ToolSource,
        call: ToolCall,
        *,
        max_turns: int,
        progress: Progress | None = None,
        builtin: Sequence[str] = (),
        files: dict[str, bytes] | None = None,
        max_fetches: int = 20,
    ) -> EngineTurn:
        """builtin: Claude Code's own tools allowed besides the agent's (WebSearch,
        WebFetch, Read), held by a BuiltinGuard; files: written in the run's folder for
        Read, named in the prompt."""
        if not self.available:
            raise SubscriptionUnavailable("the engine is paused")
        prompt = render_prompt(messages)
        sdk = self._sdk or importlib.import_module("claude_agent_sdk")
        definitions = tools.definitions()
        server = sdk.create_sdk_mcp_server(
            SERVER, tools=[_bridge(sdk, definition, call) for definition in definitions]
        )
        with self._folder(files) as (folder, note):
            turn = await self._run(
                sdk,
                system + TOOL_NAMES,
                prompt + note,
                mcp_servers={SERVER: server},
                allowed_tools=[PREFIX + d["name"] for d in definitions],
                max_turns=max_turns,
                progress=progress,
                builtin=builtin,
                cwd=folder,
                guard=BuiltinGuard(folder if files else None, set(urls_in(prompt)), max_fetches),
            )
        last = turn.messages[-1] if turn.messages else None
        if last is None or last["role"] != "assistant" or not _text_of(last["content"]):
            turn.messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": turn.text or "(stopped)"}],
                }
            )
        return turn

    async def complete(
        self,
        system: str,
        text: str,
        *,
        builtin: Sequence[str] = (),
        files: dict[str, bytes] | None = None,
        max_turns: int = 1,
    ) -> str:
        """One answer to one prompt: the server's own model calls (the knowledge base's
        curator, a web search, a scan to read) on the subscription. builtin and files as
        in run_turn; a run with tools needs more than one turn."""
        if not self.available:
            raise SubscriptionUnavailable("the engine is paused")
        sdk = self._sdk or importlib.import_module("claude_agent_sdk")
        with self._folder(files) as (folder, note):
            turn = await self._run(
                sdk,
                system,
                text + note,
                mcp_servers={},
                allowed_tools=[],
                max_turns=max_turns,
                progress=None,
                builtin=builtin,
                cwd=folder,
                guard=BuiltinGuard(folder if files else None, set(urls_in(text))),
            )
        if not turn.text:
            raise SubscriptionUnavailable("no answer")
        return turn.text

    @contextlib.contextmanager
    def _folder(self, files: dict[str, bytes] | None) -> Iterator[tuple[Path, str]]:
        """Where the run works: the config folder, or a folder of its own holding the files
        it reads, removed after; and the line that tells it which they are."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        if not files:
            yield self._config_dir, ""
            return
        folder = Path(tempfile.mkdtemp(prefix="run-", dir=self._config_dir))
        try:
            for name, data in files.items():
                (folder / Path(name).name).write_bytes(data)
            listed = "\n".join(f"- {folder / Path(name).name}" for name in files)
            yield folder, f"\n\nFiles to read (with Read):\n{listed}"
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def _run(
        self,
        sdk: Any,
        system: str,
        prompt: str,
        *,
        mcp_servers: dict[str, Any],
        allowed_tools: list[str],
        max_turns: int,
        progress: Progress | None,
        builtin: Sequence[str] = (),
        cwd: Path | None = None,
        guard: BuiltinGuard | None = None,
    ) -> EngineTurn:
        # Claude Code's working directory and its own files, on the data volume.
        self._config_dir.mkdir(parents=True, exist_ok=True)
        extra: dict[str, Any] = {}
        if builtin:
            if guard is None:
                raise SubscriptionUnavailable("Claude Code's own tools need a guard")
            extra["hooks"] = guard.hooks(sdk)
        options = sdk.ClaudeAgentOptions(
            model=self._model,
            system_prompt=system,
            tools=list(builtin),
            mcp_servers=mcp_servers,
            allowed_tools=[*allowed_tools, *builtin],
            permission_mode="dontAsk",
            setting_sources=[],
            max_turns=max_turns,
            cwd=str(cwd or self._config_dir),
            **extra,
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": self._token,
                # These outrank the login and would bill the API.
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_AUTH_TOKEN": "",
                "CLAUDE_CONFIG_DIR": str(self._config_dir),
                "DISABLE_TELEMETRY": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        turn = EngineTurn("")
        try:
            async with sdk.ClaudeSDKClient(options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    await self._take(sdk, message, turn, progress)
        except SubscriptionUnavailable:
            raise
        except Exception as exc:
            self.pause(FAILURE_PAUSE_SECONDS, f"{type(exc).__name__}: {exc}")
            raise SubscriptionUnavailable(str(exc)) from exc
        return turn

    async def _take(
        self, sdk: Any, message: Any, turn: EngineTurn, progress: Progress | None
    ) -> None:
        if isinstance(message, sdk.SystemMessage):
            if message.subtype == "init":
                source = message.data.get("apiKeySource", NO_API_KEY)
                if source != NO_API_KEY:
                    self._broken = True
                    raise SubscriptionUnavailable(
                        f"Claude Code would bill an API key ({source}), not the subscription"
                    )
        elif isinstance(message, sdk.AssistantMessage):
            content: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, sdk.TextBlock) and block.text.strip():
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, sdk.ToolUseBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name.removeprefix(PREFIX),
                            "input": block.input,
                        }
                    )
            if not content:
                return
            if text := _text_of(content):
                turn.text = text
                if progress and any(b["type"] == "tool_use" for b in content):
                    try:
                        await progress(text)
                    except Exception:
                        log.warning("Could not send a progress update", exc_info=True)
            turn.messages.append({"role": "assistant", "content": content})
        elif isinstance(message, sdk.UserMessage):
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content if block.content is not None else "",
                    **({"is_error": True} if block.is_error else {}),
                }
                for block in (message.content if isinstance(message.content, list) else [])
                if isinstance(block, sdk.ToolResultBlock)
            ]
            if not results:
                return
            if turn.messages and turn.messages[-1]["role"] == "user":
                turn.messages[-1]["content"].extend(results)
            else:
                turn.messages.append({"role": "user", "content": results})
        elif isinstance(message, sdk.ResultMessage):
            if message.subtype == "error_max_turns":
                turn.out_of_rounds = True
                return
            if message.is_error:
                error = message.result or message.subtype or "error"
                if LIMIT.search(error):
                    self.pause(LIMIT_PAUSE_SECONDS, error)
                else:
                    self.pause(FAILURE_PAUSE_SECONDS, error)
                raise SubscriptionUnavailable(error)
            if not turn.text and message.result:
                turn.text = message.result


async def subscription_first[T](
    engine: ClaudeCodeEngine | None,
    on_subscription: Callable[[ClaudeCodeEngine], Awaitable[T]],
    on_api: Callable[[], Awaitable[T]],
) -> T:
    """The user's plan first, the API when it can't take the run (no engine, paused,
    a limit, an error)."""
    if engine is not None and engine.available:
        try:
            return await on_subscription(engine)
        except SubscriptionUnavailable as exc:
            log.info("The subscription can't take this run, the API does: %s", exc)
    return await on_api()


class SubscriptionFirst:
    """An LLMClient for the server's own plain model calls (one prompt in, words out: the
    answer written from search results): the user's plan first, the API for what it can't
    take and for every request with tools or files. The engine was built for one model,
    so the request's ``model`` only reaches the API."""

    def __init__(self, engine: ClaudeCodeEngine | None, api: LLMClient) -> None:
        self._engine, self._api = engine, api

    def __getattr__(self, name: str) -> Any:
        # What else the API client offers (its file store), as it is.
        return getattr(self._api, name)

    async def create(self, **kwargs: Any) -> LLMResponse:
        prompt = None if kwargs.get("tools") else _one_prompt(kwargs.get("messages") or [])
        if prompt is None:
            return await self._api.create(**kwargs)

        async def on_subscription(engine: ClaudeCodeEngine) -> LLMResponse:
            answer = await engine.complete(_system_text(kwargs.get("system")), prompt)
            return LLMResponse("end_turn", [{"type": "text", "text": answer}])

        return await subscription_first(
            self._engine, on_subscription, lambda: self._api.create(**kwargs)
        )


def _one_prompt(messages: list[dict[str, Any]]) -> str | None:
    """The text of a request that is one user message of words only; None otherwise."""
    if len(messages) != 1 or messages[0].get("role") != "user":
        return None
    content = messages[0]["content"]
    if isinstance(content, str):
        return content
    if all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return "\n".join(b["text"] for b in content)
    return None


def _system_text(system: Any) -> str:
    if isinstance(system, str) or system is None:
        return system or ""
    return "\n\n".join(b["text"] for b in system if b.get("type") == "text")


def _bridge(sdk: Any, definition: dict[str, Any], call: ToolCall) -> Any:
    """One of the agent's tools as an MCP tool whose handler runs the registry."""
    name = definition["name"]

    async def handler(args: Any) -> dict[str, Any]:
        content, is_error = await call(name, dict(args or {}))
        result: dict[str, Any] = {"content": _mcp_content(content)}
        if is_error:
            result["is_error"] = True
        return result

    return sdk.tool(name, definition["description"], wire_schema(definition["input_schema"]))(
        handler
    )


def wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with ``properties`` always there: the SDK takes a schema without it for
    a {name: type} mapping, so {"type": "object"} would require an argument named type."""
    return {"type": "object", **schema, "properties": schema.get("properties") or {}}


def _mcp_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            blocks.append({"type": "text", "text": block["text"]})
        elif (
            isinstance(block, dict)
            and block.get("type") == "image"
            and block.get("source", {}).get("type") == "base64"
        ):
            # A picture (the screen after a click): MCP carries it as such.
            source = block["source"]
            blocks.append(
                {"type": "image", "data": source["data"], "mimeType": source["media_type"]}
            )
        else:
            blocks.append({"type": "text", "text": FILE_NOTE})
    return blocks or [{"type": "text", "text": ""}]


def render_prompt(messages: list[dict[str, Any]]) -> str:
    """The history and the new message as one prompt. A file in the new message can't be
    read here: the turn goes to the API."""
    if not messages:
        raise SubscriptionUnavailable("nothing to say")
    *history, current = messages
    text = _plain_text(current["content"], current_turn=True)
    lines = []
    for message in history:
        who = "User" if message["role"] == "user" else "Assistant"
        if rendered := _plain_text(message["content"], current_turn=False):
            lines.append(f"{who}: {rendered}")
    if not lines:
        return text
    return PROMPT.format(history="\n\n".join(lines), text=text)


def _plain_text(content: Any, *, current_turn: bool) -> str:
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            parts.append(block["text"].strip())
        elif kind == "tool_use":
            args = json.dumps(block.get("input") or {}, ensure_ascii=False)[:MAX_RESULT_CHARS]
            parts.append(f"[called {PREFIX}{block['name']} with {args}]")
        elif kind == "tool_result":
            result = block.get("content") or ""
            if not isinstance(result, str):
                result = " ".join(b.get("text", "") for b in result if isinstance(b, dict)).strip()
            parts.append(f"[result: {' '.join(result.split())[:MAX_RESULT_CHARS]}]")
        elif current_turn:
            raise FileInMessage("the message holds a file")
        else:
            parts.append("[a file]")
    return "\n".join(p for p in parts if p)


def _text_of(content: list[dict[str, Any]]) -> str:
    return " ".join(b["text"].strip() for b in content if b["type"] == "text").strip()
