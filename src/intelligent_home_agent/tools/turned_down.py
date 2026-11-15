"""Alerts the user turned down, never sent again.

A briefing or a check may raise the same alert day after day (a parcel late, an email
still unanswered, a meeting to prepare). When the user says no ("stop with that", "I
know"), the agent keeps one line here; every request then sees the list in its prompt,
beside the memory, and leaves those alerts out. Turning an alert down only makes the agent
quieter, so it runs in any request; the user sees the list (list_turned_down_alerts) and
can bring an alert back. Kept in DATA_DIR, whatever the memory's store.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

log = logging.getLogger(__name__)

MAX_ALERTS = 50
MAX_CHARS = 200


class TurnedDown:
    def __init__(self, file: Path, timezone: str) -> None:
        self._file = file
        self._tz = ZoneInfo(timezone)

    def items(self) -> list[dict[str, str]]:
        try:
            items = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("about"), str)][
            -MAX_ALERTS:
        ]

    def _save(self, items: list[dict[str, str]]) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(items[-MAX_ALERTS:], ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self._file)
        except OSError as exc:
            raise ToolError(f"The list could not be saved: {exc}") from exc

    def add(self, about: str) -> str:
        about = " ".join(about.split())[:MAX_CHARS]
        if len(about) < 4:
            raise ToolError('Say which alert, in a few words ("late parcel from the bookshop").')
        items = self.items()
        if any(i["about"].lower() == about.lower() for i in items):
            return "already turned down"
        today = datetime.now(self._tz).strftime("%Y-%m-%d")
        self._save([*items, {"about": about, "since": today}])
        return "turned down"

    def remove(self, words: str) -> int:
        needle = words.strip().lower()
        if len(needle) < 3:
            raise ToolError("Say which alert to bring back, in a few words.")
        items = self.items()
        kept = [i for i in items if needle not in i["about"].lower()]
        if len(kept) != len(items):
            self._save(kept)
        return len(items) - len(kept)

    def prompt_text(self) -> str:
        items = self.items()
        if not items:
            return ""
        lines = "\n".join(f"- {i['about']} (since {i.get('since', '?')})" for i in items)
        return (
            "Alerts the user turned down: never raise them again, in a briefing, a check "
            f"or a reply, unless they ask about them.\n{lines}"
        )


def build_turned_down_tools(turned_down: TurnedDown | None) -> list[Tool]:
    if turned_down is None:
        return []

    async def turn_down_alert(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"result": turned_down.add(str(args.get("about") or ""))}

    async def list_turned_down_alerts(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"turned_down": turned_down.items()}

    async def bring_back_alert(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"brought_back": turned_down.remove(str(args.get("words") or ""))}

    return [
        Tool(
            name="turn_down_alert",
            description=(
                'The user refuses an alert you raised ("stop with that parcel", "I know"): '
                "keep it here and never raise it again. One short line naming what it is "
                "about, specific enough not to silence other alerts."
            ),
            input_schema={
                "type": "object",
                "properties": {"about": {"type": "string"}},
                "required": ["about"],
            },
            handler=turn_down_alert,
        ),
        Tool(
            name="list_turned_down_alerts",
            description="The alerts the user turned down, with the day they did.",
            input_schema={"type": "object", "properties": {}},
            handler=list_turned_down_alerts,
        ),
        Tool(
            name="bring_back_alert",
            description="Raise again the turned-down alerts containing these words.",
            input_schema={
                "type": "object",
                "properties": {"words": {"type": "string"}},
                "required": ["words"],
            },
            handler=bring_back_alert,
        ),
    ]
