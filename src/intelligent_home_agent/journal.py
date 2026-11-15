"""A day-by-day journal of the agent's turns, kept on the data volume.

One JSON line per turn, in one file per local day: where the reply went, what the user
asked, the tools called and the errors or refusals they returned, how many messages the
agent sent while working, and the reply. A turn that read others' content (an email, a
page, a file) keeps the tools' names and errors but neither their arguments nor the
reply: those could repeat what that content said, and whatever reads the journal later (a
review, a search of past conversations) must not be steered by it. Files older than
``keep_days`` (a year) are removed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MAX_REQUEST_CHARS = 1500
MAX_REPLY_CHARS = 2000
MAX_ARGS_CHARS = 300
MAX_ERROR_CHARS = 300
KEEP_DAYS = 365


@dataclass
class Turn:
    """What one turn did, filled in by the agent as it runs."""

    display: str
    request: str
    scheduled: bool = False
    quoted: bool | str = False
    files: list[str] = field(default_factory=list)
    progress: int = 0

    def record(
        self,
        messages: list[dict[str, Any]],
        *,
        reply: str,
        tainted: bool,
        outcome: str,
        seconds: float,
        at: datetime,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "at": at.isoformat(timespec="seconds"),
            "display": self.display,
            "request": self.request[:MAX_REQUEST_CHARS],
            "tools": tool_calls(messages, with_args=not tainted),
            "progress": self.progress,
            "reply_chars": len(reply),
            "seconds": round(seconds, 1),
            "outcome": outcome,
        }
        if self.scheduled:
            entry["scheduled"] = True
        if self.quoted:
            entry["quoted"] = self.quoted
        if self.files:
            entry["files"] = self.files
        if tainted:
            entry["tainted"] = True
        else:
            entry["reply"] = reply[:MAX_REPLY_CHARS]
        return entry


def tool_calls(messages: list[dict[str, Any]], *, with_args: bool) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                call: dict[str, Any] = {"name": block.get("name", "")}
                if with_args:
                    args = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    call["args"] = args[:MAX_ARGS_CHARS]
                if block.get("id"):
                    calls[block["id"]] = call
                else:
                    unnamed.append(call)
            elif block.get("type") == "tool_result" and block.get("is_error"):
                call = calls.get(block.get("tool_use_id") or "") or {"name": "?"}
                call["error"] = _plain(block.get("content"))[:MAX_ERROR_CHARS]
    return [*calls.values(), *unnamed]


def _plain(content: Any) -> str:
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        return " ".join(
            " ".join(str(b.get("text", "")).split()) for b in content if isinstance(b, dict)
        )
    return ""


class Journal:
    def __init__(self, directory: Path, timezone: str, keep_days: int = KEEP_DAYS) -> None:
        self._dir = directory
        self._tz = ZoneInfo(timezone)
        self._keep_days = keep_days

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def _file(self, day: date) -> Path:
        return self._dir / f"{day.isoformat()}.jsonl"

    def add(self, entry: dict[str, Any]) -> None:
        """Never in the way of the turn: a journal that can't be written is only logged."""
        try:
            day = datetime.fromisoformat(entry["at"]).date()
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._file(day).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._prune(day)
        except (OSError, KeyError, ValueError, TypeError) as exc:
            log.warning("Could not write the journal: %s", exc)

    def day(self, day: date) -> list[dict[str, Any]]:
        try:
            lines = self._file(day).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def _prune(self, today: date) -> None:
        cutoff = (today - timedelta(days=self._keep_days)).isoformat()
        for path in self._dir.glob("*.jsonl"):
            if path.stem < cutoff:
                path.unlink(missing_ok=True)
