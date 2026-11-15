from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import respx
from test_config_crypt import KEY

from intelligent_home_agent.config_crypt import ConfigCrypt
from intelligent_home_agent.tools import ReminderRepo, build_assistant_reminder_tools
from intelligent_home_agent.tools.assistant_reminders import is_due, known_every, parse, runs_on
from intelligent_home_agent.tools.github import API_URL

BASE = f"{API_URL}/repos/me/config/contents/reminders"

DUE = """---
title: Propose the team meeting
on: 2000-01-01
until: 2999-12-31
where: office
machine: work-laptop
group: Example Corp
---
Agenda and invite.
"""
LATER = "---\ntitle: Not yet\non: 2999-01-01\n---\nLater.\n"
EXPIRED = "---\ntitle: Too late\non: 2000-01-01\nuntil: 2000-01-02\n---\nOld.\n"
TODAY = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
CLOSED_TODAY = f"---\ntitle: Daily\non: 2000-01-01\nevery: day\ndone: {TODAY}\n---\nDone.\n"
FILES = {"due.md": DUE, "later.md": LATER, "expired.md": EXPIRED, "daily.md": CLOSED_TODAY}
PARIS = ZoneInfo("Europe/Paris")


def listing(files, **shas):
    return [{"name": n, "type": "file", "sha": shas.get(n, "")} for n in files]


@respx.mock
async def test_lists_only_due_reminders(ctx):
    entries = listing(FILES) + [{"name": "past", "type": "dir"}]
    route = respx.get(BASE).mock(return_value=httpx.Response(200, json=entries))
    for name, text in FILES.items():
        respx.get(f"{BASE}/{name}").mock(return_value=httpx.Response(200, text=text))
    async with httpx.AsyncClient() as http:
        (tool,) = build_assistant_reminder_tools(ReminderRepo(http, "ghp_test", "me/config"), "UTC")
        result = await tool.handler({}, ctx)

    assert result == {
        "count": 1,
        "reminders": [
            {
                "id": "due",
                "title": "Propose the team meeting",
                "until": "2999-12-31",
                "where": "office",
                "machine": "work-laptop",
                "group": "Example Corp",
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


@respx.mock
async def test_a_file_is_downloaded_again_only_when_it_changed():
    respx.get(BASE).mock(
        side_effect=[
            httpx.Response(200, json=listing(["a.md", "b.md"], **{"a.md": "1", "b.md": "1"})),
            httpx.Response(200, json=listing(["a.md", "b.md"], **{"a.md": "1", "b.md": "2"})),
        ]
    )
    a = respx.get(f"{BASE}/a.md").mock(return_value=httpx.Response(200, text="A"))
    b = respx.get(f"{BASE}/b.md").mock(return_value=httpx.Response(200, text="B"))
    async with httpx.AsyncClient() as http:
        repo = ReminderRepo(http, "t", "me/config")
        assert await repo.named_files() == {"a.md": "A", "b.md": "B"}
        assert await repo.named_files() == {"a.md": "A", "b.md": "B"}
    assert a.call_count == 1 and b.call_count == 2


@respx.mock
async def test_an_encrypted_folder_is_read_with_the_key_and_left_out_without():
    crypt = ConfigCrypt.from_base64(KEY, ["reminders/**"])
    respx.get(BASE).mock(return_value=httpx.Response(200, json=listing(["a.md", "b.md"])))
    respx.get(f"{BASE}/a.md").mock(
        return_value=httpx.Response(200, content=crypt.encrypt(DUE.encode()))
    )
    respx.get(f"{BASE}/b.md").mock(return_value=httpx.Response(200, text=LATER))
    async with httpx.AsyncClient() as http:
        with_key = await ReminderRepo(http, "t", "me/config", crypt=crypt).named_files()
        without = await ReminderRepo(http, "t", "me/config").named_files()
    assert with_key == {"a.md": DUE, "b.md": LATER}
    # Never shown as ciphertext, and never in the way of the files that can be read.
    assert without == {"b.md": LATER}


def test_due_rules():
    now = datetime(2026, 10, 20, 9, 0, tzinfo=PARIS)
    assert is_due({"on": "2026-10-20"}, now)
    assert is_due({"on": "2026-10-20 08:59", "until": "2026-10-20"}, now)
    assert not is_due({"on": "2026-10-20 09:01"}, now)
    assert not is_due({"on": "2026-10-19", "until": "2026-10-19"}, now)


def test_a_repeating_reminder_is_due_only_on_its_days_from_its_time():
    tuesday = datetime(2026, 10, 20, 9, 0, tzinfo=PARIS)
    friday = datetime(2026, 10, 23, 9, 0, tzinfo=PARIS)
    weekly = {"on": "2026-10-01", "every": "friday"}
    assert not is_due(weekly, tuesday)
    assert is_due(weekly, friday)
    assert not is_due({**weekly, "done": "2026-10-23"}, friday)
    assert is_due({**weekly, "done": "2026-10-16"}, friday)
    assert not is_due({**weekly, "on": "2026-10-01 14:00"}, friday)
    assert not is_due({**weekly, "times": "10:00, 16:00"}, friday)
    assert not is_due({**weekly, "until": "2026-10-22"}, friday)
    assert not is_due({**weekly, "on": "2026-10-30"}, friday)
    assert not is_due({"on": "oops", "every": "day"}, friday)


def test_repeat_days():
    thursday, friday = date(2026, 10, 22), date(2026, 10, 23)
    assert runs_on("monday, thursday", None, thursday)
    assert not runs_on("monday, thursday", None, friday)
    assert runs_on("weekday", None, friday) and not runs_on("weekday", None, date(2026, 10, 24))
    assert runs_on("day", None, friday)
    assert runs_on("3 days", thursday, date(2026, 10, 25))
    assert not runs_on("3 days", thursday, date(2026, 10, 24))
    assert runs_on("month", date(2026, 10, 31), date(2026, 11, 30))
    assert runs_on("month", date(2026, 10, 31), date(2026, 12, 31))
    assert not runs_on("month", date(2026, 10, 31), date(2026, 12, 30))
    assert all(known_every(e) for e in ("day", "weekday", "mon, thursday", "3 days", "month"))
    assert not any(known_every(e) for e in ("", "fortnight", "0 days", "monday, soon"))


def test_parse_front_matter():
    meta, body = parse("---\r\ntitle: A: b\r\n---\r\nText\r\n")
    assert meta == {"title": "A: b"}
    assert body == "Text"


def test_no_repo_means_no_tool():
    assert build_assistant_reminder_tools(None, "UTC") == []
