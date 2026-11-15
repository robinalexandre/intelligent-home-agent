from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from intelligent_home_agent.icloud_web import ICloudWeb
from intelligent_home_agent.tools import ToolError, build_note_tools
from intelligent_home_agent.tools.apple_notes import ICloudNotes


def summary(note_id, title, modified, folder="Notes", locked=False, deleted=False, snippet=""):
    return SimpleNamespace(
        id=note_id,
        title=title,
        snippet=snippet,
        folder_name=folder,
        modified_at=datetime(2026, 10, modified, tzinfo=UTC),
        is_locked=locked,
        is_deleted=deleted,
    )


class FakeNotes:
    def __init__(self, items, texts):
        self.feed = items
        self.texts = texts
        self.fetched = []
        self.cursors = []

    def sync_cursor(self):
        return f"c{len(self.cursors) + 1}"

    def iter_all(self, since=None):
        self.cursors.append(since)
        items, self.feed = self.feed, []
        return items

    def get(self, note_id, with_attachments=False):
        self.fetched.append(note_id)
        attachments = (
            [SimpleNamespace(filename="quote.pdf", uti=None)] if with_attachments else None
        )
        return SimpleNamespace(
            text=self.texts[note_id],
            modified_at=datetime(2026, 10, int(note_id[1:]), tzinfo=UTC),
            attachments=attachments,
        )


def tools_for(service):
    api = SimpleNamespace(
        notes=service, data={"ok": 1}, requires_2fa=False, is_trusted_session=True
    )
    source = ICloudNotes(ICloudWeb("sam@example.com", "unused", opener=lambda *_: api))
    search, read = build_note_tools(source, "Europe/Rome")
    return search, read


ITEMS = [
    summary("n3", "Baking", 3, folder="Pantry"),
    summary("n5", "Shopping", 5, snippet="bread, milk"),
    summary("n4", "Codes", 4, locked=True),
    summary("n2", "Old", 2, deleted=True),
]
TEXTS = {"n3": "Small tin above 20 scones", "n5": "bread, milk, eggs", "n4": "secret"}


async def test_recent_notes_first_without_query(ctx):
    search, _ = tools_for(FakeNotes(list(ITEMS), TEXTS))

    result = await search.handler({}, ctx)

    assert [n["title"] for n in result["notes"]] == ["Shopping", "Codes", "Baking"]
    assert result["folders"] == ["Notes", "Pantry"]
    assert result["notes"][0]["start"] == "bread, milk"
    assert search.taints


async def test_search_looks_inside_texts_but_never_opens_locked_notes(ctx):
    service = FakeNotes(list(ITEMS), TEXTS)
    search, _ = tools_for(service)

    result = await search.handler({"query": "20 TIN"}, ctx)
    assert [n["title"] for n in result["notes"]] == ["Baking"]
    assert "n4" not in service.fetched

    await search.handler({"query": "eggs"}, ctx)
    assert sorted(service.fetched) == ["n3", "n5"]  # texts kept until the note changes


async def test_read_note_gives_text_and_attachments(ctx):
    _, read = tools_for(FakeNotes(list(ITEMS), TEXTS))

    result = await read.handler({"id": "n3"}, ctx)

    assert result["text"] == "Small tin above 20 scones" and result["attachments"] == ["quote.pdf"]
    assert result["folder"] == "Pantry" and result["truncated"] is False
    with pytest.raises(ToolError, match="locked"):
        await read.handler({"id": "n4"}, ctx)
    with pytest.raises(ToolError, match="search_apple_notes"):
        await read.handler({"id": "n2"}, ctx)
