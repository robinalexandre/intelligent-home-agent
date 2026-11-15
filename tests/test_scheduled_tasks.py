import base64
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tasks import built_in_tasks
from intelligent_home_agent.tools import (
    LocalFolder,
    NotesRepo,
    ReminderRepo,
    RepoFolder,
    ToolContext,
    ToolError,
    ToolRegistry,
    build_task_tools,
)
from intelligent_home_agent.tools.github import API_URL

CONTENTS = f"{API_URL}/repos/me/config/contents"
TODAY = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")
RECAP = (
    "---\ntitle: Evening recap\non: 2026-10-24 20:00\nevery: day\ndigest: yes\n"
    "done: 2026-10-29\n---\n\nGive me the evening recap.\n"
)


def local(tmp_path, language="en", **options):
    folder = LocalFolder(tmp_path / "tasks")
    return {t.name: t for t in build_task_tools(folder, "UTC", language, **options)}, folder


def remote(http):
    folder = RepoFolder(
        NotesRepo(http, "ghp_test", "me/config", author="nova"),
        ReminderRepo(http, "ghp_test", "me/config", "nova/tasks"),
        "nova/tasks",
    )
    return {t.name: t for t in build_task_tools(folder, "UTC")}


def served(text):
    content = base64.b64encode(text.encode()).decode()
    return httpx.Response(200, json={"sha": "abc", "content": content})


def written(route, n=0):
    body = json.loads(route.calls[n].request.content)
    return body["message"], base64.b64decode(body["content"]).decode()


def test_no_folder_means_no_tools_and_writes_take_a_clean_request(tmp_path):
    assert build_task_tools(None, "UTC") == []
    tools, _ = local(tmp_path, reaches="a push notification")
    assert list(tools) == [
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "delete_scheduled_task",
    ]
    assert "reaches them as a push notification" in tools["schedule_task"].description
    assert not tools["list_scheduled_tasks"].taints
    update, delete = tools["update_scheduled_task"], tools["delete_scheduled_task"]
    assert tools["schedule_task"].needs_clean_turn and tools["schedule_task"].parks
    # Only the user's own message changes or deletes a task.
    assert update.needs_clean_turn and update.needs_user_request and not update.proposable
    assert delete.needs_user_request and delete.always_propose and delete.proposable


async def test_the_agent_schedules_lists_changes_and_deletes_a_local_task(tmp_path, ctx):
    tools, folder = local(tmp_path)
    result = await tools["schedule_task"].handler(
        {
            "title": "Call the plumber",
            "at": "2026-10-23 18:00",
            "every": "Monday,  Thursday",
            "times": "08:00, 18:00",
            "request": "Remind me to call the plumber.",
        },
        ctx,
    )
    task = f"{TODAY}-call-the-plumber"
    assert result == {"scheduled": task}
    text = (tmp_path / "tasks" / f"{task}.md").read_text()
    assert text.startswith(
        "---\ntitle: Call the plumber\non: 2026-10-23 18:00\nevery: monday, thursday\n"
        "times: 08:00, 18:00\ncreated: "
    )
    assert text.endswith("---\n\nRemind me to call the plumber.\n")

    listed = await tools["list_scheduled_tasks"].handler({}, ctx)
    assert listed == {
        "count": 1,
        "tasks": [
            {
                "id": task,
                "title": "Call the plumber",
                "at": "2026-10-23 18:00",
                "every": "monday, thursday",
                "times": "08:00, 18:00",
                "until": None,
                "request": "Remind me to call the plumber.",
            }
        ],
    }

    update = tools["update_scheduled_task"]
    assert await update.handler(
        {"id": task, "at": "2026-10-24 19:00", "request": "Remind me,\nand say why."}, ctx
    ) == {"updated": task}
    text = await folder.text(f"{task}.md")
    assert "\non: 2026-10-24 19:00\nevery: monday, thursday\n" in text
    assert text.endswith("---\n\nRemind me,\nand say why.\n")
    with pytest.raises(ToolError, match="at least one"):
        await update.handler({"id": task}, ctx)
    with pytest.raises(ToolError, match="id is"):
        await update.handler({"id": "../memory", "title": "y"}, ctx)

    delete = tools["delete_scheduled_task"]
    assert await delete.summary({"id": task}) == (
        f"Delete the scheduled task “Call the plumber” ({task}) for good"
    )
    assert await delete.handler({"id": task}, ctx) == {"deleted": task}
    with pytest.raises(ToolError, match="No "):
        await delete.handler({"id": task}, ctx)


async def test_a_repeat_or_a_time_the_runner_would_not_understand_is_refused(tmp_path, ctx):
    tools, folder = local(tmp_path)
    task = {"title": "Wind", "at": "2026-11-13 08:00", "request": "Check the wind"}
    for bad in ({"every": "fortnight"}, {"times": "8am"}, {"at": "tomorrow"}, {"request": " "}):
        with pytest.raises(ToolError):
            await tools["schedule_task"].handler({**task, **bad}, ctx)
    with pytest.raises(ToolError, match="key or token"):
        await tools["schedule_task"].handler({**task, "request": "use ghp_abcdefghijklmnop"}, ctx)
    assert await folder.named_files() == {}


async def test_a_task_asked_for_after_a_read_waits_for_the_users_go(tmp_path, ctx):
    tools, folder = local(tmp_path, "fr")
    registry = ToolRegistry(list(tools.values()))
    ctx.read_page("example.com")
    task = {"title": "Wind", "at": "2026-11-13 08:00", "request": "Check the wind\nand the tide"}
    parked = await registry.run("schedule_task", task, ctx)
    assert parked["status"] == "waiting_for_the_user"
    assert ctx.actions[0]["summary"] == (
        "Tâche planifiée — title: Wind · at: 2026-11-13 08:00 · "
        "request: Check the wind ⏎ and the tide"
    )
    assert await folder.named_files() == {}
    # A task never changes or deletes a task: that takes the user's own message.
    scheduled = ToolContext(Conversation(id="t"), scheduled=True)
    waits = await registry.run("update_scheduled_task", {"id": "x", "title": "y"}, scheduled)
    assert waits["status"] == "waiting_for_the_user"
    one_way = ToolContext(Conversation(id="t"), scheduled=True, answerable=False)
    with pytest.raises(ToolError, match="nobody can answer"):
        await registry.run("update_scheduled_task", {"id": "x", "title": "y"}, one_way)


async def test_the_briefing_from_the_settings_is_listed_but_changed_in_the_settings(tmp_path, ctx):
    built_in = built_in_tasks(strings("en"), "07:30", None)
    tools, folder = local(tmp_path, built_in=built_in)
    listed = await tools["list_scheduled_tasks"].handler({}, ctx)
    assert listed["tasks"] == [
        {
            "id": "morning-briefing",
            "title": "Morning briefing",
            "at": "2000-01-01 07:30",
            "every": "day",
            "times": None,
            "until": None,
            "request": "Give me the morning briefing",
            "from_settings": True,
        }
    ]
    for name in ("update_scheduled_task", "delete_scheduled_task"):
        with pytest.raises(ToolError, match="MORNING_BRIEFING_AT"):
            await tools[name].handler({"id": "morning-briefing", "title": "x"}, ctx)
    # A file of that name replaces it, and is then a task like any other.
    await folder.put("morning-briefing", built_in["morning-briefing.md"].replace("07:30", "06:00"))
    listed = await tools["list_scheduled_tasks"].handler({}, ctx)
    assert listed["count"] == 1 and "from_settings" not in listed["tasks"][0]
    assert listed["tasks"][0]["at"] == "2000-01-01 06:00"
    assert await tools["delete_scheduled_task"].handler({"id": "morning-briefing"}, ctx)


@respx.mock
async def test_tasks_in_a_repository_folder(ctx):
    created = respx.put(f"{CONTENTS}/nova/tasks/{TODAY}-call-the-plumber.md").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.get(f"{CONTENTS}/nova/tasks").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"type": "file", "name": "recap.md", "sha": "1"},
                {"type": "dir", "name": "past"},
            ],
        )
    )
    respx.get(f"{CONTENTS}/nova/tasks/recap.md").mock(
        side_effect=lambda request: (
            httpx.Response(200, text=RECAP) if "raw" in request.headers["accept"] else served(RECAP)
        )
    )
    updated = respx.put(f"{CONTENTS}/nova/tasks/recap.md").mock(
        return_value=httpx.Response(200, json={})
    )
    deleted = respx.delete(f"{CONTENTS}/nova/tasks/recap.md").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{CONTENTS}/nova/tasks/none.md").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        tools = remote(http)
        scheduled = await tools["schedule_task"].handler(
            {"title": "Call the plumber", "at": "2026-10-23 18:00", "request": "Remind me."}, ctx
        )
        listed = await tools["list_scheduled_tasks"].handler({}, ctx)
        await tools["update_scheduled_task"].handler({"id": "recap", "at": "2026-10-24 21:00"}, ctx)
        assert await tools["delete_scheduled_task"].handler({"id": "recap"}, ctx)
        with pytest.raises(ToolError, match="none"):
            await tools["delete_scheduled_task"].handler({"id": "none"}, ctx)

    assert scheduled == {"scheduled": f"{TODAY}-call-the-plumber"}
    message, text = written(created)
    assert message == f"chore(sync): add nova/tasks/{TODAY}-call-the-plumber.md from nova"
    assert text.endswith("---\n\nRemind me.\n")
    assert [t["id"] for t in listed["tasks"]] == ["recap"]  # past ones are not listed
    message, text = written(updated)
    assert message == "chore(sync): update nova/tasks/recap.md from nova"
    assert text == RECAP.replace("20:00", "21:00")
    assert json.loads(updated.calls[0].request.content)["sha"] == "abc"
    assert json.loads(deleted.calls[0].request.content)["sha"] == "abc"


@respx.mock
async def test_a_repository_task_is_closed_as_a_reminder_is():
    original = "---\ntitle: Call\non: 2026-10-23 18:00\nevery: day\n---\n\nRemind me.\n"
    respx.get(f"{CONTENTS}/nova/tasks/a.md").mock(return_value=served(original))
    added = respx.put(f"{CONTENTS}/nova/tasks/past/a.md").mock(
        return_value=httpx.Response(201, json={})
    )
    in_place = respx.put(f"{CONTENTS}/nova/tasks/a.md").mock(
        return_value=httpx.Response(200, json={})
    )
    removed = respx.delete(f"{CONTENTS}/nova/tasks/a.md").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as http:
        folder = RepoFolder(
            NotesRepo(http, "t", "me/config"), ReminderRepo(http, "t", "me/config"), "nova/tasks"
        )
        await folder.close_for_day("a.md", "2026-10-23")
        moved = await folder.close("a.md", "2026-10-23 18:01")

    assert "\ndone: 2026-10-23\n---" in written(in_place)[1]
    assert moved == "nova/tasks/past/a.md"
    assert written(added)[1] == original.replace(
        "every: day\n", "every: day\nclosed: 2026-10-23 18:01\n"
    )
    assert removed.called
