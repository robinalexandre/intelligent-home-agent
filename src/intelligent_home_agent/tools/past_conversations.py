"""The day's journal and the past conversations, searchable.

The journal (intelligent_home_agent.journal) keeps every turn, one file per day: what the
user asked, the tools used and, for a request that read nothing of others', the reply.
Here the agent reads one day of it ("what did we do today?", a recap) and searches all of
it ("what we said about the quote for the terrace"). A turn that read an email or a page
kept no reply; one quoting another message may repeat what an email said, so reading it
taints the request like that email would.
"""

from __future__ import annotations

import unicodedata
from datetime import date, timedelta
from typing import Any

from intelligent_home_agent.journal import Journal
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_FOUND = 15
MAX_DAY_TURNS = 60
MAX_SHOWN_REQUEST = 400
MAX_SHOWN_REPLY = 700
MAX_DAYS = 365


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(plain.lower().split())


def _shown(entry: dict[str, Any]) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "at": entry.get("at", "")[:16].replace("T", " "),
        "asked": entry.get("request", "")[:MAX_SHOWN_REQUEST],
        "tools": sorted({c.get("name", "") for c in entry.get("tools", [])} - {""}),
    }
    if entry.get("reply"):
        turn["replied"] = entry["reply"][:MAX_SHOWN_REPLY]
    elif entry.get("tainted"):
        turn["replied"] = "(reply not kept: that request read an email, a file or a page)"
    if entry.get("scheduled"):
        turn["scheduled_task"] = True
    return turn


def _others_words(entry: dict[str, Any]) -> bool:
    return bool(entry.get("files")) or entry.get("quoted") not in (None, False, "own")


def build_past_conversation_tools(journal: Journal | None) -> list[Tool]:
    if journal is None:
        return []

    def carry(ctx: ToolContext, entries: list[dict[str, Any]]) -> None:
        if any(_others_words(e) for e in entries):
            ctx.tainted = True

    async def read_day_journal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        raw = str(args.get("date") or "").strip()
        try:
            day = date.fromisoformat(raw) if raw else journal.now().date()
        except ValueError as exc:
            raise ToolError("date is YYYY-MM-DD, or empty for today.") from exc
        # Not the review's own turns: a day read back is what the user asked.
        entries = [e for e in journal.day(day) if e.get("display") != "review"]
        carry(ctx, entries)
        return {
            "date": day.isoformat(),
            "turns": len(entries),
            "journal": [_shown(e) for e in entries[-MAX_DAY_TURNS:]],
        }

    async def search_past_conversations(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        words = [w for w in _fold(str(args.get("query") or "")).split() if len(w) > 2]
        if not words:
            raise ToolError('Give a few words of what was said, e.g. "terrace quote".')
        try:
            days = min(max(int(args.get("days") or 90), 1), MAX_DAYS)
        except (TypeError, ValueError) as exc:
            raise ToolError("days is a number of days.") from exc
        today = journal.now().date()
        found: list[dict[str, Any]] = []
        for back in range(days):
            for entry in reversed(journal.day(today - timedelta(days=back))):
                text = _fold(f"{entry.get('request', '')} {entry.get('reply', '')}")
                if all(w in text for w in words):
                    found.append(entry)
            if len(found) >= MAX_FOUND:
                break
        found = found[:MAX_FOUND]
        carry(ctx, found)
        return {"found": [_shown(e) for e in found], "searched_days": days}

    return [
        Tool(
            name="read_day_journal",
            description=(
                "The journal of one day (today by default): each request the user made, "
                'the tools used and your reply, in order. For "what did we do today?", the '
                "evening recap, or to find where a job stopped."
            ),
            input_schema={
                "type": "object",
                "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            },
            handler=read_day_journal,
        ),
        Tool(
            name="search_past_conversations",
            description=(
                "Search the past conversations (as far back as the journal is kept, a year "
                'at most) for words the user or you said: "what we said about the quote '
                'for the terrace", "the number I gave you for the insurance". Every word '
                "must appear; newest first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "days": {"type": "integer", "description": "How far back; default 90."},
                },
                "required": ["query"],
            },
            handler=search_past_conversations,
        ),
    ]
