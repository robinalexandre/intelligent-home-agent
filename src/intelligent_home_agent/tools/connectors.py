"""Connectors: the remote MCP servers the user plugs in (a bank, a hosting provider, an
accounting or rental tool...), each listed in ``MCP_SERVERS`` (docs/connectors.md).

A server may have a hundred tools with long schemas, which would weigh on every request,
so the model gets a few per connector: ``<name>_help`` (what the server offers),
``<name>_read``, and, when the connector allows writes, ``<name>_write``.

What each server tool is comes from its own annotations: one not flagged read-only is a
write. The rules are enforced here, not left to the prompt:

- a read taints the request, as an account holds text other people wrote (a payment's
  label, a customer's message): nothing acts on the home or leaves it for its rest
  without the user's go;
- a write never runs when asked: it is parked like every write that waits for the user
  (``always_propose``, tools/proposals.py), the code lists the exact call under the
  reply, and it only runs through run_proposals, when the user says yes in a later
  message, in a request that read nothing else;
- with ``"writes": "off"`` (the default) the write tools don't exist at all.
"""

from __future__ import annotations

import json
import re
from typing import Any

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.mcp_client import MCPClient

MAX_RESULT_CHARS = 12_000
MAX_HELP_CHARS = 8_000
MAX_SHOWN_CHARS = 600
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def is_read(spec: dict[str, Any], also_read: frozenset[str] = frozenset()) -> bool:
    hints = spec.get("annotations") or {}
    return hints.get("readOnlyHint") is True or spec.get("name") in also_read


def is_destructive(spec: dict[str, Any]) -> bool:
    # MCP's default for a tool that writes is destructive: only an explicit False is harmless.
    return (spec.get("annotations") or {}).get("destructiveHint") is not False


def _summary(description: str) -> str:
    return " ".join(description.split()).split(". ", 1)[0][:160]


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[cut at {limit} characters: ask for less, e.g. a filter or a page]"


class Connector:
    def __init__(
        self,
        name: str,
        client: MCPClient,
        *,
        writes: bool = False,
        also_read: frozenset[str] = frozenset(),
        language: str = "en",
    ) -> None:
        self.name, self.label, self.writes = name, client.label, writes
        self._language = language
        self._client, self._also_read = client, also_read
        self._specs: dict[str, dict[str, Any]] | None = None

    async def specs(self) -> dict[str, dict[str, Any]]:
        """The server's tools, asked once, at the first use rather than at startup."""
        if self._specs is None:
            listed = await self._client.list_tools()
            self._specs = {
                str(s["name"]): s for s in listed if _TOOL_NAME.match(str(s.get("name") or ""))
            }
        return self._specs

    def _kind(self, spec: dict[str, Any]) -> str:
        return "read" if is_read(spec, self._also_read) else "write"

    async def checked(self, args: dict[str, Any], kind: str) -> tuple[str, dict[str, Any]]:
        specs = await self.specs()
        name = str(args.get("tool") or "")
        if name not in specs:
            raise ToolError(f"{self.label} has no tool {name!r}: see {self.name}_help.")
        actual = self._kind(specs[name])
        if actual != kind:
            if actual == "write" and not self.writes:
                raise ToolError(f"{name} changes something, and {self.label} is read-only here.")
            raise ToolError(f"{name} is a {actual}: use {self.name}_{actual}.")
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments are the tool's own arguments, as an object.")
        return name, arguments

    async def help(self, args: dict[str, Any], _: ToolContext) -> str:
        specs = await self.specs()
        wanted = [str(n) for n in args.get("tools") or []]
        if not wanted:
            return "\n".join(
                f"{name} ({self._kind(spec)}): {_summary(str(spec.get('description') or ''))}"
                for name, spec in sorted(specs.items())
                if self.writes or self._kind(spec) == "read"
            )
        parts = []
        for name in wanted:
            spec = specs.get(name)
            if spec is None:
                parts.append(f"## {name}\nNo such tool.")
                continue
            schema = json.dumps(spec.get("inputSchema") or {}, ensure_ascii=False)
            parts.append(
                f"## {name} ({self._kind(spec)})\n{spec.get('description') or ''}\n"
                f"Arguments (JSON schema): {schema}"
            )
        return _cut("\n\n".join(parts), MAX_HELP_CHARS * len(wanted))

    async def read(self, args: dict[str, Any], _: ToolContext) -> str:
        name, arguments = await self.checked(args, "read")
        return _cut(await self._client.call(name, arguments), MAX_RESULT_CHARS)

    async def vet_write(self, args: dict[str, Any], _: ToolContext) -> None:
        """Refused before it is parked: the user is never asked about a call that is no
        write of this server, or that can't be shown to them whole."""
        _, arguments = await self.checked(args, "write")
        shown = _shown(arguments)
        if len(shown) > MAX_SHOWN_CHARS or not shown.isprintable():
            raise ToolError(
                "The arguments are too long, or hold hidden characters, to be shown to the "
                "user for confirmation."
            )

    async def describe_write(self, args: dict[str, Any]) -> str:
        """The exact call, as the user approves it."""
        s = strings(self._language)
        name = str(args.get("tool") or "")
        spec = (await self.specs()).get(name) or {}
        return s.connector_write_summary.format(
            label=self.label,
            warning=s.may_destroy if is_destructive(spec) else "",
            tool=name,
            arguments=_shown(args.get("arguments") or {}),
        )

    async def write(self, args: dict[str, Any], _: ToolContext) -> str:
        # Only reached on the user's go: the registry parks every call first.
        name, arguments = await self.checked(args, "write")
        return _cut(await self._client.call(name, arguments), MAX_RESULT_CHARS)


def _shown(arguments: Any) -> str:
    return json.dumps(arguments, ensure_ascii=False, separators=(", ", ": "))


def _call_schema(what: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": f"The server tool to {what}."},
            "arguments": {"type": "object", "description": "Its arguments, as its help says."},
        },
        "required": ["tool"],
    }


def build_connector_tools(connectors: list[Connector]) -> list[Tool]:
    tools: list[Tool] = []
    for c in connectors:
        tools.append(
            Tool(
                name=f"{c.name}_help",
                description=(
                    f"What {c.label} offers: without arguments, its tools in one line each; "
                    "with `tools`, their full description and arguments. Call it before "
                    f"{c.name}_read"
                    + (f" or {c.name}_write" if c.writes else "")
                    + " when unsure of a tool or its arguments."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"tools": {"type": "array", "items": {"type": "string"}}},
                },
                handler=c.help,
            )
        )
        tools.append(
            Tool(
                name=f"{c.name}_read",
                description=f"Read from {c.label} with one of its read tools ({c.name}_help).",
                input_schema=_call_schema("read with"),
                handler=c.read,
                taints=True,
            )
        )
        if c.writes:
            tools.append(
                Tool(
                    name=f"{c.name}_write",
                    description=(
                        f"Change something in {c.label} with one of its write tools. It "
                        "never runs at once: it waits for the user's go, and they see the "
                        "exact call under your reply. run_proposals does it once they agree."
                    ),
                    input_schema=_call_schema("write with"),
                    handler=c.write,
                    needs_clean_turn=True,
                    proposable=True,
                    always_propose=True,
                    describe=c.describe_write,
                    vet=c.vet_write,
                )
            )
    return tools
