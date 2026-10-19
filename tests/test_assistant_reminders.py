from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import respx

from intelligent_home_agent.tools import ReminderRepo, build_assistant_reminder_tools
from intelligent_home_agent.tools.assistant_reminders import is_due, parse
from intelligent_home_agent.tools.github import API_URL

BASE = f"{API_URL}/repos/me/config/contents/reminders"

DUE = """---
title: Propose the team meeting
on: 2000-01-01
until: 2999-12-31
where: office
machine: work-laptop
---
Agenda and invite.
"""
LATER = "---\ntitle: Not yet\non: 2999-01-01\n---\nLater.\n"
EXPIRED = "---\ntitle: Too late\non: 2000-01-01\nuntil: 2000-01-02\n---\nOld.\n"
FILES = {"due.md": DUE, "later.md": LATER, "expired.md": EXPIRED}


@respx.mock
async def test_lists_only_due_reminders(ctx):
    listing = [{"name": n, "type": "file"} for n in FILES] + [{"name": "done", "type": "dir"}]
    route = respx.get(BASE).mock(return_value=httpx.Response(200, json=listing))
    for name, text in FILES.items():
        respx.get(f"{BASE}/{name}").mock(return_value=httpx.Response(200, text=text))
    async with httpx.AsyncClient() as http:
        (tool,) = build_assistant_reminder_tools(ReminderRepo(http, "ghp_test", "me/config"), "UTC")
        result = await tool.handler({}, ctx)

    assert result == {
        "count": 1,
        "reminders": [
            {
                "title": "Propose the team meeting",
                "until": "2999-12-31",
                "where": "office",
                "machine": "work-laptop",
                "details": "Agenda and invite.",
            }
        ],
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer ghp_test"
    assert tool.taints


@respx.mock
async def test_missing_folder_means_no_reminders(ctx):
    respx.get(BASE).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        (tool,) = build_assistant_reminder_tools(ReminderRepo(http, "t", "me/config"), "UTC")
        assert await tool.handler({}, ctx) == {"count": 0, "reminders": []}


def test_due_rules():
    now = datetime(2026, 10, 20, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
    assert is_due({"on": "2026-10-20"}, now)
    assert is_due({"on": "2026-10-20 08:59", "until": "2026-10-20"}, now)
    assert not is_due({"on": "2026-10-20 09:01"}, now)
    assert not is_due({"on": "2026-10-19", "until": "2026-10-19"}, now)


def test_parse_front_matter():
    meta, body = parse("---\r\ntitle: A: b\r\n---\r\nText\r\n")
    assert meta == {"title": "A: b"}
    assert body == "Text"


def test_no_repo_means_no_tool():
    assert build_assistant_reminder_tools(None, "UTC") == []
