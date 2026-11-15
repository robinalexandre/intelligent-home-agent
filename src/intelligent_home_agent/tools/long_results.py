"""Results too long for one read, kept apart and read part by part.

A long email, a page, a repository file or an API answer used to be cut at a few
thousand characters, and the end was lost. Now the agent gets the first part with the
number of parts, and read_result_part reads the next ones from the conversation's store.

A part carries what its whole carried: one from an email or a file taints the request
that reads it, one from a page counts as that site read, one from a search as the web
read. The store keeps the last few results, in memory only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

# One part: what most results fit in, and a size a turn can read several times.
PART_CHARS = 12_000
MAX_KEPT = 12


@dataclass
class LongResult:
    tool: str
    text: str
    # What reading it means for the request, as the tool that produced it did.
    private: bool = False
    web: bool = False
    hosts: frozenset[str] = field(default_factory=frozenset)

    @property
    def parts(self) -> int:
        return -(-len(self.text) // PART_CHARS)

    def part(self, n: int) -> str:
        return self.text[(n - 1) * PART_CHARS : n * PART_CHARS]


def keep_long(conv: Any, result: LongResult) -> str:
    """Keep a long result; returns its id (r1, r2...)."""
    store: dict[str, LongResult] = conv.long_results
    conv.long_result_count += 1
    key = f"r{conv.long_result_count}"
    store[key] = result
    while len(store) > MAX_KEPT:
        store.pop(next(iter(store)))
    return key


def first_part(key: str, result: LongResult) -> str:
    return (
        f"{result.part(1)}\n\n[Result {key} of {result.tool}: part 1 of {result.parts} "
        f"({len(result.text)} characters). The rest is kept: read_result_part with id {key} "
        "and the part you need, only if the answer is not already here.]"
    )


def build_long_result_tools() -> list[Tool]:
    async def read_result_part(args: dict[str, Any], ctx: ToolContext) -> str:
        key = str(args.get("id") or "").strip()
        result = ctx.conversation.long_results.get(key)
        if result is None:
            raise ToolError(f"No kept result {key}: it may have expired; call the tool again.")
        try:
            n = int(args.get("part") or 2)
        except (TypeError, ValueError) as exc:
            raise ToolError("part is a number, e.g. 2.") from exc
        if not 1 <= n <= result.parts:
            raise ToolError(f"{key} has parts 1 to {result.parts}.")
        # What the whole was for the request, its part is too.
        if result.private:
            ctx.tainted = True
        if result.web:
            ctx.web_read = True
        for host in result.hosts:
            ctx.read_page(host)
        return f"[Result {key}, part {n} of {result.parts}]\n{result.part(n)}"

    return [
        Tool(
            name="read_result_part",
            description=(
                "Read another part of a tool result that was too long to come whole (its "
                "first part ends with its id and how many parts it has): a long email, "
                "page, file or list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The result's id, e.g. r3."},
                    "part": {"type": "integer", "description": "From 2 to its last part."},
                },
                "required": ["id", "part"],
            },
            handler=read_result_part,
        )
    ]
