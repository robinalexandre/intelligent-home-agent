"""How-tos that worked: the steps of a job, kept to do it again without searching (a
bill fetched from a provider's site, a booking, a report built from a connected service).

One Markdown file beside the memory (same store: a local file or a path in a GitHub
repository, docs/memory.md), one section per site and job, readable and editable by the
user. Saving one is a write: after a read (the job itself read the site's pages, so nearly
always) it waits for the user's go, shown with every step, so a page can't plant a
"procedure" that would steer later requests. Reading them is not a read of others'
content: every word was shown to the user first.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, describe_whole
from intelligent_home_agent.tools.memory import PASSWORD, SECRET, Memory

MAX_STEPS_CHARS = 3000
MAX_FOUND = 5
_SECTION = re.compile(r"^## ", re.MULTILINE)


def procedures_header(assistant: str) -> str:
    return (
        f"# {assistant} — procedures that worked\n\n"
        f"Steps {assistant} followed successfully, to do the job again. One section per "
        "site and job; edit or delete freely.\n"
    )


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(plain.lower().split())


def sections(text: str) -> list[str]:
    return [f"## {part.strip()}" for part in _SECTION.split(text)[1:] if part.strip()]


def build_procedure_tools(
    book: Memory | None, timezone: str, assistant: str = "Assistant", language: str = "en"
) -> list[Tool]:
    """``book``: the procedures file, a Memory built on its own path."""
    if book is None:
        return []
    header = procedures_header(assistant)

    async def save_procedure(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        site = " ".join(str(args.get("site") or "").split())
        job = " ".join(str(args.get("job") or "").split())
        steps = str(args.get("steps") or "").strip()
        if not (site and job and steps):
            raise ToolError("site, job and steps are required.")
        if len(steps) > MAX_STEPS_CHARS:
            raise ToolError(f"Keep the steps under {MAX_STEPS_CHARS} characters.")
        if PASSWORD.search(steps) or SECRET.search(steps):
            raise ToolError("Never keep passwords, keys or tokens in a procedure.")
        today = datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")
        title = f"## {site} — {job}"
        section = f"{title}\n\n_Worked on {today}._\n\n{steps}\n"

        def put(text: str) -> tuple[str, str]:
            if not text.strip():
                text = header
            kept = [s for s in sections(text) if _fold(s.splitlines()[0]) != _fold(title)]
            replaced = len(kept) < len(sections(text))
            head = _SECTION.split(text)[0].rstrip("\n")
            body = "\n\n".join([*kept, section.rstrip("\n")])
            return f"{head}\n\n{body}\n", "updated" if replaced else "saved"

        message = f"chore(memory): {assistant} keeps how to {job}"
        return {"result": await book.update(put, message)}

    async def find_procedure(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        words = [w for w in _fold(str(args.get("query") or "")).split() if len(w) > 2]
        if not words:
            raise ToolError('Say the site or the job, e.g. "energy bill".')
        text = await book.read()
        scored = []
        for section in sections(text):
            folded = _fold(section)
            score = sum(w in folded for w in words) + 2 * sum(
                w in _fold(section.splitlines()[0]) for w in words
            )
            if score:
                scored.append((score, section))
        scored.sort(key=lambda s: -s[0])
        return {"procedures": [s for _, s in scored[:MAX_FOUND]]}

    return [
        Tool(
            name="find_procedure",
            description=(
                "Before a job of several steps (fetching a bill from a provider's site, a "
                "booking, a report from a connected service...), look up how it was done "
                "successfully before: the steps kept with save_procedure. Follow them, and "
                "adapt if things changed."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Site and/or job."}},
                "required": ["query"],
            },
            handler=find_procedure,
        ),
        Tool(
            name="save_procedure",
            description=(
                "Once a job of several steps worked (and only then), keep its steps to do "
                "it again: where to start from, what to call, click or type, where the "
                "result is, the traps met. Numbered lines, no password, no personal "
                "number. Saving the same site and job again replaces the old steps. After a "
                "read it waits for the user's go, shown whole."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "site": {
                        "type": "string",
                        "description": "The site or service, e.g. energy.example.com",
                    },
                    "job": {"type": "string", "description": "e.g. download the monthly bill"},
                    "steps": {"type": "string", "description": "1. ...\n2. ..."},
                },
                "required": ["site", "job", "steps"],
            },
            handler=save_procedure,
            needs_clean_turn=True,
            describe=describe_whole(strings(language).procedure_summary),
            multiline_args=frozenset({"steps"}),
        ),
    ]
