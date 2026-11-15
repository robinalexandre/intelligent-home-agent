"""The language model behind the agent, behind one small interface.

The agent loop, the conversation memory and the tools all speak one message format: the
Messages format (a list of ``{"role", "content"}`` messages whose content is a string or
a list of blocks: ``text``, ``tool_use``, ``tool_result``, and ``image`` or ``document``
for the files the user sends or a tool returns). A provider takes a request in
that format and returns an :class:`LLMResponse` in that format, so nothing else in the
code depends on a vendor SDK.

Two clients are shipped:

- :class:`AnthropicClient`, the Messages API as it is;
- :class:`OpenAICompatibleClient`, the Chat Completions API that OpenAI, Mistral, Groq,
  OpenRouter, Gemini and local servers (Ollama, LM Studio, llama.cpp, vLLM) all speak.
  :data:`OPENAI_COMPATIBLE` lists the ones known by name; any other server works with
  ``LLM_PROVIDER=openai-compatible`` and ``LLM_BASE_URL``.

To add a provider with another API, write a class with an async
``create(**kwargs) -> LLMResponse`` that converts ``system``, ``tools`` (``name``,
``description``, ``input_schema``) and ``messages`` to the vendor's format and the answer
back, map its stop reason to ``end_turn`` / ``tool_use`` / ``max_tokens``, then make
:func:`build_llm_client` return it. See AGENTS.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

ANTHROPIC = "anthropic"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_ANTHROPIC_SEARCH_MODEL = "claude-haiku-5-5"
CUSTOM = "openai-compatible"


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    # Local servers take any key, or none.
    needs_key: bool = True


# Chat Completions endpoints known by name. Model names change often and differ from one
# to the next, so none is assumed: LLM_MODEL is required for all of them.
OPENAI_COMPATIBLE: dict[str, Endpoint] = {
    "openai": Endpoint("https://api.openai.com/v1"),
    "mistral": Endpoint("https://api.mistral.ai/v1"),
    "groq": Endpoint("https://api.groq.com/openai/v1"),
    "openrouter": Endpoint("https://openrouter.ai/api/v1"),
    "gemini": Endpoint("https://generativelanguage.googleapis.com/v1beta/openai"),
    "ollama": Endpoint("http://localhost:11434/v1", needs_key=False),
    "lmstudio": Endpoint("http://localhost:1234/v1", needs_key=False),
}
PROVIDERS = (ANTHROPIC, *OPENAI_COMPATIBLE, CUSTOM)
# Providers that run the server-side web search tool used by tools/web.py.
WEB_SEARCH_PROVIDERS = {ANTHROPIC}


def needs_key(provider: str) -> bool:
    if provider == CUSTOM:
        return False
    return provider == ANTHROPIC or OPENAI_COMPATIBLE[provider].needs_key


class LLMError(Exception):
    """The provider refused the request or answered something unusable."""


@dataclass
class LLMResponse:
    # "end_turn", "tool_use", "max_tokens", or "pause_turn" (a server-side tool such as
    # web search paused and must be sent back unchanged).
    stop_reason: str
    content: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    """Accepts ``model``, ``max_tokens``, ``system`` (text, or text blocks), ``tools``,
    ``messages`` and, optionally, ``tool_choice`` (``{"type": "none"}``). Blocks may carry
    a ``cache_control`` marker, which a provider without prompt caching ignores."""

    async def create(self, **kwargs: Any) -> LLMResponse: ...


class AnthropicClient:
    def __init__(self, api_key: str) -> None:
        from anthropic import AsyncAnthropic

        self._messages = AsyncAnthropic(api_key=api_key).messages

    async def create(self, **kwargs: Any) -> LLMResponse:
        response = await self._messages.create(**kwargs)
        return LLMResponse(
            stop_reason=response.stop_reason or "end_turn",
            content=[block.model_dump(exclude_none=True) for block in response.content],
        )


class OpenAICompatibleClient:
    """Chat Completions over plain HTTP: no vendor SDK, any server that speaks it."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 120.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = http or httpx.AsyncClient(timeout=timeout)
        # OpenAI's own recent models refuse max_tokens; the other servers only know it.
        self._limit_key = "max_completion_tokens" if "api.openai.com" in base_url else "max_tokens"

    async def create(self, **kwargs: Any) -> LLMResponse:
        body: dict[str, Any] = {
            "model": kwargs["model"],
            self._limit_key: kwargs["max_tokens"],
            "messages": to_chat_messages(kwargs.get("system"), kwargs["messages"]),
        }
        if tools := kwargs.get("tools"):
            body["tools"] = [to_chat_tool(tool) for tool in tools]
            # {"type": "none"}: answer in words, as when the agent is out of tool rounds.
            if (kwargs.get("tool_choice") or {}).get("type") == "none":
                body["tool_choice"] = "none"
        try:
            response = await self._http.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"The model server can't be reached: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            # The body says why (unknown model, no tool support...), never the key.
            raise LLMError(
                f"The model server answered {response.status_code}: {response.text[:300]}"
            )
        try:
            choice = response.json()["choices"][0]
            return from_chat_choice(choice)
        except (ValueError, LookupError, TypeError, AttributeError) as exc:
            raise LLMError("The model server's answer isn't a chat completion.") from exc


def to_chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    if "input_schema" not in tool:
        # A provider's own server-side tool (e.g. Anthropic's web search) has no equivalent.
        raise LLMError(f"Tool {tool.get('name')!r} only exists at another provider.")
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
        },
    }


def to_chat_messages(
    system: str | list[dict[str, Any]] | None, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Messages format to Chat Completions: tool calls and results become their own shape.

    What only the Messages API knows is carried as well as the other format allows: cache
    markers are dropped, an image becomes an ``image_url`` data URL (for models that see),
    a PDF document becomes a note saying it can't be read, and an image inside a tool
    result follows it in a user message, as a tool message only holds text.
    """
    out: list[dict[str, Any]] = []
    if system:
        if not isinstance(system, str):
            system = "\n\n".join(block.get("text", "") for block in system)
        out.append({"role": "system", "content": system})
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            texts = [block["text"] for block in content if block.get("type") == "text"]
            calls = [
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
                for block in content
                if block.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        # Results must directly follow the assistant message that made the calls.
        seen: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") == "tool_result":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": _result_text(block),
                    }
                )
                inner = block.get("content")
                if isinstance(inner, list):
                    seen += [_image_part(b) for b in inner if b.get("type") == "image"]
        parts = [part for block in content if (part := _user_part(block)) is not None]
        if seen:
            parts = [{"type": "text", "text": "[Image returned by the tool above]"}, *seen, *parts]
        if not parts:
            continue
        if all(part["type"] == "text" for part in parts):
            out.append({"role": "user", "content": "\n".join(part["text"] for part in parts)})
        else:
            out.append({"role": "user", "content": parts})
    return out


def _image_part(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source") or {}
    url = source.get("url") or f"data:{source.get('media_type')};base64,{source.get('data')}"
    return {"type": "image_url", "image_url": {"url": url}}


def _document_note(block: dict[str, Any]) -> str:
    name = block.get("title") or "A document"
    return (
        f"[{name}: a PDF this model can't read here; it can still be attached or passed to "
        "a tool by its name.]"
    )


def _user_part(block: dict[str, Any]) -> dict[str, Any] | None:
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": block["text"]}
    if kind == "image":
        return _image_part(block)
    if kind == "document":
        return {"type": "text", "text": _document_note(block)}
    return None


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content", "")
    if not isinstance(content, str):
        texts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "document":
                texts.append(_document_note(part))
            elif part.get("text"):
                texts.append(part["text"])
        content = "\n".join(texts)
    return f"Error: {content}" if block.get("is_error") else content


def from_chat_choice(choice: dict[str, Any]) -> LLMResponse:
    message = choice["message"]
    content: list[dict[str, Any]] = []
    if text := (message.get("content") or "").strip():
        content.append({"type": "text", "text": text})
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call["function"]
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append(
            {
                "type": "tool_use",
                # Some local servers leave the id out.
                "id": call.get("id") or f"call_{index}",
                "name": function["name"],
                "input": args if isinstance(args, dict) else {},
            }
        )
    if any(block["type"] == "tool_use" for block in content):
        # Some servers say "stop" even when the answer holds tool calls.
        return LLMResponse("tool_use", content)
    stop = "max_tokens" if choice.get("finish_reason") == "length" else "end_turn"
    return LLMResponse(stop, content)


def build_llm_client(
    provider: str, api_key: str | None = None, base_url: str | None = None
) -> LLMClient:
    if provider == ANTHROPIC:
        if not api_key:
            raise ValueError("LLM_PROVIDER=anthropic needs LLM_API_KEY")
        return AnthropicClient(api_key)
    if provider == CUSTOM:
        if not base_url:
            raise ValueError("LLM_PROVIDER=openai-compatible needs LLM_BASE_URL")
        return OpenAICompatibleClient(base_url, api_key)
    try:
        endpoint = OPENAI_COMPATIBLE[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider!r}; available: {', '.join(PROVIDERS)}"
        ) from None
    if endpoint.needs_key and not api_key:
        raise ValueError(f"LLM_PROVIDER={provider} needs LLM_API_KEY")
    return OpenAICompatibleClient(base_url or endpoint.base_url, api_key)
