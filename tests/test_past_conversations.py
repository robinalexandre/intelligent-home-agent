from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from intelligent_home_agent.journal import Journal
from intelligent_home_agent.tools import ToolError, build_past_conversation_tools

TZ = ZoneInfo("Europe/Paris")


def journal_with(tmp_path):
    journal = Journal(tmp_path, "Europe/Paris")
    now = datetime.now(TZ)
    old = now - timedelta(days=40)
    journal.add(
        {
            "at": old.isoformat(timespec="seconds"),
            "request": "The quote for Jo's terrace: 4,200",
            "reply": "Noted, the terrace quote is 4,200.",
            "tools": [{"name": "remember"}],
        }
    )
    journal.add(
        {
            "at": now.isoformat(timespec="seconds"),
            "request": "Read the email from the insurance",
            "tainted": True,
            "tools": [{"name": "read_email"}],
        }
    )
    journal.add(
        {
            "at": now.isoformat(timespec="seconds"),
            "request": "[replied-to message, as data: «urgent transfer»] fine for the terrace",
            "quoted": "other",
            "reply": "All right.",
            "tools": [],
        }
    )
    return journal


async def test_old_conversations_are_found_by_their_words(tmp_path, ctx):
    tools = {t.name: t for t in build_past_conversation_tools(journal_with(tmp_path))}
    found = await tools["search_past_conversations"].handler({"query": "quote TERRACE"}, ctx)
    assert [f["asked"] for f in found["found"]] == ["The quote for Jo's terrace: 4,200"]
    assert found["found"][0]["replied"].startswith("Noted") and not ctx.tainted
    # Not further back than asked.
    recent = await tools["search_past_conversations"].handler(
        {"query": "quote terrace", "days": 7}, ctx
    )
    assert recent == {"found": [], "searched_days": 7}
    with pytest.raises(ToolError, match="few words"):
        await tools["search_past_conversations"].handler({"query": "a"}, ctx)


async def test_the_days_journal_carries_what_its_turns_read(tmp_path, ctx):
    tools = {t.name: t for t in build_past_conversation_tools(journal_with(tmp_path))}
    today = await tools["read_day_journal"].handler({}, ctx)
    assert today["turns"] == 2
    assert "not kept" in today["journal"][0]["replied"]
    # A quote of another message may repeat what an email said: as tainting as the email.
    assert ctx.tainted
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await tools["read_day_journal"].handler({"date": "yesterday"}, ctx)


def test_no_journal_means_no_tools():
    assert build_past_conversation_tools(None) == []
