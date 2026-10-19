"""Tool abstraction shared by every capability the agent exposes to the model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from intelligent_home_agent.conversations import Conversation


class ToolError(Exception):
    """An error message meant to be shown to the model (not a crash)."""


@dataclass
class ToolContext:
    conversation: Conversation
    # Things the client must do on the user's phone after replying (e.g. create a reminder).
    actions: list[dict[str, Any]] = field(default_factory=list)
    # Set once this request has read private, third-party-written content (emails, events,
    # pull requests).
    tainted: bool = False
    # Event places read from the calendar in this request: the only destinations travel
    # time accepts once the request is tainted.
    calendar_places: set[str] = field(default_factory=set)


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    # Output is private content written by others (emails, invites): it can carry
    # injected instructions, and must not leak out.
    taints: bool = False
    # Acts on the home or sends data out: refused once the request is tainted.
    needs_clean_turn: bool = False

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def extend(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.to_api() for tool in self._tools.values()]

    def names(self) -> set[str]:
        return set(self._tools)

    def taints(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and tool.taints

    async def run(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        if tool.needs_clean_turn and ctx.tainted:
            raise ToolError(
                f"{name} is disabled in a request that read emails, the calendar, GitHub or "
                "assistant reminders. "
                "Tell the user to ask for it again in a separate request."
            )
        return await tool.handler(args, ctx)
