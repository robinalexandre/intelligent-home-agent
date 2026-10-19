"""The language model behind the agent, behind one small interface.

The agent loop, the conversation memory and the tools all speak one message format: the
Messages format (a list of ``{"role", "content"}`` messages whose content is a string or
a list of blocks: ``text``, ``tool_use``, ``tool_result``). A provider takes a request in
that format and returns an :class:`LLMResponse` in that format, so nothing else in the
code depends on a vendor SDK.

Anthropic is the only provider shipped. To add one (OpenAI-compatible, a local model...),
write a class with an async ``create(**kwargs) -> LLMResponse`` that converts ``system``,
``tools`` (``name``, ``description``, ``input_schema``) and ``messages`` to the vendor's
format and the answer back, map its stop reason to ``end_turn`` / ``tool_use`` /
``max_tokens``, then register it in :data:`PROVIDERS`. See AGENTS.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMResponse:
    # "end_turn", "tool_use", "max_tokens", or "pause_turn" (a server-side tool such as
    # web search paused and must be sent back unchanged).
    stop_reason: str
    content: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    """Accepts ``model``, ``max_tokens``, ``system``, ``tools`` and ``messages``."""

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


PROVIDERS: dict[str, Callable[[str], LLMClient]] = {"anthropic": AnthropicClient}
# Providers that run the server-side web search tool used by tools/web.py.
WEB_SEARCH_PROVIDERS = {"anthropic"}


def build_llm_client(provider: str, api_key: str) -> LLMClient:
    try:
        factory = PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"Unknown LLM_PROVIDER {provider!r}; available: {', '.join(PROVIDERS)}"
        ) from None
    return factory(api_key)
