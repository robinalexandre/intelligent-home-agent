"""The agent's own tools on the tasks it runs by itself (tasks.py, docs/tasks.md).

A task is one Markdown file of a folder (NoteFolder: the agent's disk, or a repository).
The agent only writes one in a request that read nothing else, so nothing it reads (an
email, a web page) can give it a task; changing or deleting one takes the user's own
message.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.assistant_notes import (
    NoteFolder,
    date_arg,
    front_matter,
    note_id,
    one_line,
    text_arg,
    with_body,
    with_field,
)
from intelligent_home_agent.tools.assistant_reminders import known_every, parse
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, describe_whole

_TIMES = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(\s*,\s*([01]\d|2[0-3]):[0-5]\d)*$")


def every_arg(args: dict[str, Any]) -> str:
    every = one_line(str(args.get("every") or "")).lower()
    if every and not known_every(every):
        raise ToolError(
            'every is "day", "weekday", week days ("monday, thursday"), "3 days" or "month".'
        )
    return every


def times_arg(args: dict[str, Any]) -> str:
    times = one_line(str(args.get("times") or ""))
    if times and not _TIMES.match(times):
        raise ToolError('times is one or several "HH:MM", e.g. "08:00, 14:00".')
    return times


def build_task_tools(
    folder: NoteFolder | None,
    timezone: str,
    language: str = "en",
    built_in: dict[str, str] | None = None,
    reaches: str = "a notification",
) -> list[Tool]:
    """``built_in``: the tasks that come from the settings (the briefing and the recap at
    their time), by file name: listed with the others, changed in the settings.
    ``reaches``: how a task's result reaches the user, for the model."""
    if folder is None:
        return []
    s = strings(language)
    built_in = built_in or {}

    def now() -> datetime:
        return datetime.now(ZoneInfo(timezone))

    def task_name(args: dict[str, Any]) -> str:
        name = f"{note_id(args.get('id'), 'list_scheduled_tasks')}.md"
        return name

    async def stored(name: str) -> str:
        if name in built_in and name not in await folder.named_files():
            raise ToolError(
                f"{name.removesuffix('.md')} comes from the agent's settings "
                "(MORNING_BRIEFING_AT, EVENING_RECAP_AT): the user changes its time or turns "
                "it off there, or replaces it with a task file of the same name."
            )
        return await folder.text(name)

    async def schedule_task(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        title, request = text_arg(args, "title"), text_arg(args, "request")
        stamp = now()
        text = front_matter(
            {
                "title": title,
                "on": date_arg(args, "at", required=True),
                "until": date_arg(args, "until"),
                "every": every_arg(args) or None,
                "times": times_arg(args) or None,
                "created": stamp.strftime("%Y-%m-%d %H:%M"),
            }
        )
        name = await folder.create(title, f"{text}\n{request}\n", f"{stamp:%Y-%m-%d}")
        return {"scheduled": name.removesuffix(".md")}

    async def list_scheduled_tasks(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        files = await folder.named_files()
        tasks = []
        for name, text in sorted(({**built_in, **files}).items()):
            meta, body = parse(text)
            task = {
                "id": name.removesuffix(".md"),
                "title": meta.get("title"),
                "at": meta.get("on"),
                "every": meta.get("every") or None,
                "times": meta.get("times") or None,
                "until": meta.get("until") or None,
                "request": body,
            }
            if name not in files:
                task["from_settings"] = True
            tasks.append(task)
        return {"count": len(tasks), "tasks": tasks}

    async def update_scheduled_task(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        name = task_name(args)
        changes = {
            "title": str(args.get("title") or "").strip(),
            "on": date_arg(args, "at"),
            "every": every_arg(args),
            "times": times_arg(args),
            "until": date_arg(args, "until"),
        }
        request = str(args.get("request") or "").strip()
        if not request and not any(changes.values()):
            raise ToolError(
                "Give at least one of title, at, every, times, until or request to change."
            )
        text = await stored(name)
        for key, value in changes.items():
            if value:
                text = with_field(text, key, one_line(value))
        if request:
            text = with_body(text, request)
        await folder.put(name, text)
        return {"updated": name.removesuffix(".md")}

    async def delete_scheduled_task(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        name = task_name(args)
        await stored(name)
        if not await folder.delete(name):
            raise ToolError(f"No scheduled task {args.get('id')}: see list_scheduled_tasks.")
        return {"deleted": name.removesuffix(".md")}

    async def describe_delete_task(args: dict[str, Any]) -> str:
        task = str(args.get("id") or "")
        try:
            title = parse(await folder.text(task_name(args)))[0].get("title")
        except ToolError:
            title = None
        label = s.note_label.format(title=title, id=task) if title else task
        return s.task_delete_summary.format(label=label)

    id_property = {"type": "string", "description": "The id list_scheduled_tasks gave."}
    every_property = {
        "type": "string",
        "description": 'To repeat: "day", "weekday", "monday, thursday", "3 days" or "month".',
    }
    times_property = {
        "type": "string",
        "description": 'A repeating task, several times a day: "08:00, 14:00".',
    }
    return [
        Tool(
            name="schedule_task",
            description=(
                "Schedule something you will do by yourself later: a reminder to send at a "
                "time, a check, a piece of work. At `at` (within a minute) you get `request` "
                f"as if the user had just written it, and your reply reaches them as {reaches}. "
                "With `every`, it repeats at the time of `at` (or at each of `times`) until "
                "`until`, and stays silent when it has nothing new."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One line, user's language."},
                    "at": {
                        "type": "string",
                        "description": 'When to start: "YYYY-MM-DD HH:MM" or "YYYY-MM-DD".',
                    },
                    "until": {
                        "type": "string",
                        "description": "Last day it may still start, if missed (optional).",
                    },
                    "every": every_property,
                    "times": times_property,
                    "request": {
                        "type": "string",
                        "description": (
                            "What to do then, written as the user's request, with every "
                            'detail needed (e.g. "Remind me to call the plumber about the '
                            'quote").'
                        ),
                    },
                },
                "required": ["title", "at", "request"],
            },
            handler=schedule_task,
            needs_clean_turn=True,
            describe=describe_whole(s.task_label),
            multiline_args=frozenset({"request"}),
        ),
        Tool(
            name="list_scheduled_tasks",
            description=(
                "The tasks you will run by yourself later, the user's and yours (briefing, "
                "recap, checks...): id, title, start, repeat days, last day and the request "
                "you get when it runs."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=list_scheduled_tasks,
        ),
        Tool(
            name="update_scheduled_task",
            description=(
                "Change one of your scheduled tasks, by its id from list_scheduled_tasks, "
                "instead of scheduling a second one: only the fields given change. `request` "
                "replaces the whole text, so pass it whole with the change in it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": id_property,
                    "title": {"type": "string"},
                    "at": {
                        "type": "string",
                        "description": 'New start, and time of day: "YYYY-MM-DD HH:MM".',
                    },
                    "every": every_property,
                    "times": times_property,
                    "until": {"type": "string", "description": "New last day."},
                    "request": {"type": "string", "description": "The new whole request."},
                },
                "required": ["id"],
            },
            handler=update_scheduled_task,
            needs_clean_turn=True,
            describe=describe_whole(s.task_update_label),
            multiline_args=frozenset({"request"}),
            needs_user_request=True,
        ),
        Tool(
            name="delete_scheduled_task",
            description=(
                "Delete one of your scheduled tasks for good, by its id from "
                "list_scheduled_tasks, only when the user asks to remove it. It always waits "
                "for their go: call it right away."
            ),
            input_schema={
                "type": "object",
                "properties": {"id": id_property},
                "required": ["id"],
            },
            handler=delete_scheduled_task,
            describe=describe_delete_task,
            needs_clean_turn=True,
            needs_user_request=True,
            proposable=True,
            always_propose=True,
        ),
    ]
