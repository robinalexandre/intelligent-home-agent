"""The agent loop: a language model + tools, one conversation turn at a time."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.llm import LLMClient
from intelligent_home_agent.prompts import PromptConfig, build_system_prompt
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class AgentReply:
    text: str
    actions: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        prompt: PromptConfig,
        *,
        model: str,
        max_tokens: int,
        max_tool_iterations: int,
        max_history_messages: int,
    ) -> None:
        self._client = llm
        self._tools = tools
        self._prompt = prompt
        # The prompt only describes the tools that are actually registered.
        self._prompt.tools = tools.names()
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_tool_iterations
        self._max_history = max_history_messages

    async def ask(
        self,
        conv: Conversation,
        text: str,
        context: str | None = None,
        *,
        notification: bool = False,
    ) -> AgentReply:
        """Run one user turn to completion and return the reply.

        ``context`` is extra data the client sends along (e.g. today's reminders from the
        phone); it is passed to the model as data, not as part of the request.
        ``notification`` asks for a reply written to be read rather than heard.
        """
        conv.turn += 1
        if context:
            text = f"{text}\n\n<phone_context>\n{context}\n</phone_context>"
        user_message = {"role": "user", "content": text}
        start = len(conv.messages)
        conv.messages.append(user_message)
        ctx = ToolContext(conversation=conv)
        try:
            reply = await self._run_turn(conv, ctx, notification) or self._fallback()
        except BaseException:
            # Never keep a half-finished turn: it may hold private content or dangling
            # tool results that would break the next request.
            del conv.messages[start:]
            raise

        if ctx.tainted:
            # Emails and events stay out of memory once answered: only the question and the
            # spoken reply are kept, so text injected in them can't steer later requests.
            conv.messages[start:] = [
                user_message,
                {"role": "assistant", "content": [{"type": "text", "text": reply}]},
            ]
        conv.trim(self._max_history)
        if ctx.to_confirm:
            asked = strings(self._prompt.language).to_confirm
            reply = "\n".join([reply, "", asked, *(f"- {line}" for line in ctx.to_confirm)])
        return AgentReply(reply, ctx.actions)

    async def _run_turn(
        self, conv: Conversation, ctx: ToolContext, notification: bool
    ) -> str | None:
        system = build_system_prompt(self._prompt, notification=notification)
        for _ in range(self._max_iterations):
            response = await self._client.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                tools=self._tools.definitions(),
                messages=conv.messages,
            )
            content = response.content
            conv.messages.append({"role": "assistant", "content": content})
            if response.stop_reason != "tool_use":
                return _text_of(content)

            calls = [block for block in content if block["type"] == "tool_use"]
            # Calls in one batch were all decided before any of them ran, so the taint only
            # applies from the next batch on.
            results = await asyncio.gather(*(self._run_tool(block, ctx) for block in calls))
            if any(self._tools.taints(call["name"]) for call in calls):
                ctx.tainted = True
            conv.messages.append({"role": "user", "content": list(results)})

        log.warning("Tool loop limit reached in conversation %s", conv.id)
        # The history must not end with dangling tool results.
        conv.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "(stopped)"}]}
        )
        return None

    def _fallback(self) -> str:
        return strings(self._prompt.language).fallback_reply

    async def _run_tool(self, block: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name, args = block["name"], block.get("input") or {}
        log.info("tool call %s %s", name, json.dumps(args, ensure_ascii=False))
        try:
            result = await self._tools.run(name, args, ctx)
            return {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        except ToolError as exc:
            return _error_result(block["id"], str(exc))
        except Exception:
            log.exception("tool %s crashed", name)
            return _error_result(block["id"], "Internal error while running this tool.")


def _error_result(tool_use_id: str, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}


def _text_of(content: list[dict[str, Any]]) -> str:
    return " ".join(b["text"].strip() for b in content if b["type"] == "text").strip()
