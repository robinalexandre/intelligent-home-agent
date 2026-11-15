"""The user's Apple Notes, read only, through iCloud's web services (icloud_web).

iCloud can't search notes: their titles and first lines are read from the change feed
(whole once, then what changed), and the text of the most recent ones is fetched and kept
until they change, so a search also looks inside them. Locked notes stay closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

if TYPE_CHECKING:
    from intelligent_home_agent.icloud_web import ICloudWeb

MAX_LISTED = 20
# Notes whose text a search reads, most recent first.
MAX_SEARCHED_TEXTS = 100
MAX_TEXT_CHARS = 20_000


@dataclass(frozen=True)
class NoteSummary:
    id: str
    title: str
    snippet: str
    folder: str
    modified: datetime | None
    locked: bool = False


@dataclass(frozen=True)
class NoteText:
    summary: NoteSummary
    text: str
    attachments: list[str]


class NoteSource(Protocol):
    async def summaries(self) -> list[NoteSummary]: ...

    async def texts(self, ids: list[str]) -> dict[str, str]: ...

    async def read(self, note_id: str) -> NoteText: ...


def _summary(item: Any) -> NoteSummary:
    return NoteSummary(
        id=item.id,
        title=" ".join((item.title or "").split()) or "(untitled)",
        snippet=" ".join((item.snippet or "").split()),
        folder=item.folder_name or "",
        modified=item.modified_at,
        locked=bool(item.is_locked),
    )


class ICloudNotes:
    def __init__(self, icloud: ICloudWeb) -> None:
        self._icloud = icloud
        self._items: dict[str, Any] = {}
        self._cursor: str | None = None
        self._texts: dict[str, tuple[datetime | None, str]] = {}

    def _sync(self, api: Any) -> None:
        service = api.notes
        cursor = service.sync_cursor()
        for item in service.iter_all(since=self._cursor):
            if item.is_deleted:
                self._items.pop(item.id, None)
            else:
                self._items[item.id] = item
        self._cursor = cursor

    def _text(self, api: Any, note_id: str) -> str:
        item = self._items.get(note_id)
        cached = self._texts.get(note_id)
        if item is not None and cached is not None and cached[0] == item.modified_at:
            return cached[1]
        note = api.notes.get(note_id)
        text = note.text or ""
        self._texts[note_id] = (note.modified_at, text)
        return text

    async def summaries(self) -> list[NoteSummary]:
        def fetch(api: Any) -> list[NoteSummary]:
            self._sync(api)
            return [_summary(item) for item in self._items.values()]

        return await self._icloud.run(fetch)

    async def texts(self, ids: list[str]) -> dict[str, str]:
        def fetch(api: Any) -> dict[str, str]:
            return {i: self._text(api, i) for i in ids}

        return await self._icloud.run(fetch)

    async def read(self, note_id: str) -> NoteText:
        def fetch(api: Any) -> NoteText:
            self._sync(api)
            item = self._items.get(note_id)
            if item is None:
                raise ToolError(f"No note {note_id}: search_apple_notes gives the ids.")
            if item.is_locked:
                raise ToolError("This note is locked: only the user's devices can open it.")
            note = api.notes.get(note_id, with_attachments=True)
            self._texts[note_id] = (note.modified_at, note.text or "")
            names = [a.filename or a.uti or "attachment" for a in note.attachments or []]
            return NoteText(_summary(item), note.text or "", names)

        return await self._icloud.run(fetch)


def build_note_tools(source: NoteSource, timezone: str) -> list[Tool]:
    tz = ZoneInfo(timezone)

    def shown(n: NoteSummary) -> dict[str, Any]:
        return {
            "id": n.id,
            "title": n.title,
            "folder": n.folder,
            "modified": n.modified.astimezone(tz).isoformat(timespec="minutes")
            if n.modified
            else None,
            "start": n.snippet[:200],
            "locked": n.locked,
        }

    def recent_first(notes: list[NoteSummary]) -> list[NoteSummary]:
        return sorted(
            notes, key=lambda n: n.modified or datetime.min.replace(tzinfo=tz), reverse=True
        )

    async def search_apple_notes(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        notes = recent_first(await source.summaries())
        folders = sorted({n.folder for n in notes if n.folder})
        folder = str(args.get("folder") or "").casefold()
        if folder:
            notes = [n for n in notes if n.folder.casefold() == folder]
        words = str(args.get("query") or "").casefold().split()
        if words:
            readable = [n.id for n in notes if not n.locked][:MAX_SEARCHED_TEXTS]
            texts = await source.texts(readable)

            def matches(n: NoteSummary) -> bool:
                haystack = f"{n.title} {n.snippet} {n.folder} {texts.get(n.id, '')}".casefold()
                return all(word in haystack for word in words)

            notes = [n for n in notes if matches(n)]
        return {
            "count": len(notes),
            "folders": folders,
            "notes": [shown(n) for n in notes[:MAX_LISTED]],
        }

    async def read_apple_note(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        note = await source.read(str(args.get("id") or ""))
        text = note.text
        return {
            **shown(note.summary),
            "text": text[:MAX_TEXT_CHARS],
            "truncated": len(text) > MAX_TEXT_CHARS,
            "attachments": note.attachments,
        }

    return [
        Tool(
            name="search_apple_notes",
            description=(
                "The user's Apple Notes (iCloud): without query, the most recent ones; with "
                "query, those holding every word of it, in their title or text. Gives each "
                "note's id, title, folder, last change and first line, and the folder names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to find, any order."},
                    "folder": {"type": "string", "description": "Only this folder, by name."},
                },
            },
            handler=search_apple_notes,
            taints=True,
        ),
        Tool(
            name="read_apple_note",
            description="The whole text of one of the user's Apple Notes, by its id from "
            "search_apple_notes, with the names of its attachments.",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The id search_apple_notes gave."}
                },
                "required": ["id"],
            },
            handler=read_apple_note,
            taints=True,
        ),
    ]
