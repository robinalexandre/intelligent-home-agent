"""General-purpose tools that don't depend on Home Assistant."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_REMINDERS_PER_TURN = 5


def build_general_tools(timezone: str) -> list[Tool]:
    tz = ZoneInfo(timezone)

    async def get_current_datetime(_: dict[str, Any], __: ToolContext) -> dict[str, Any]:
        now = datetime.now(tz)
        return {
            "iso": now.isoformat(timespec="minutes"),
            "weekday": now.strftime("%A"),
            "timezone": timezone,
        }

    async def create_reminder(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        title = (args.get("title") or "").strip()
        if not title:
            raise ToolError("A reminder needs a title.")
        if sum(a["type"] == "reminder" for a in ctx.actions) >= MAX_REMINDERS_PER_TURN:
            raise ToolError(f"At most {MAX_REMINDERS_PER_TURN} reminders per request.")
        due = None
        if raw_due := args.get("due"):
            try:
                parsed = datetime.fromisoformat(raw_due)
            except ValueError as exc:
                raise ToolError("due must be an ISO date-time, e.g. 2026-10-15T10:00") from exc
            due = (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).isoformat()
        ctx.actions.append(
            {"type": "reminder", "title": title[:200], "due": due, "notes": args.get("notes")}
        )
        return {"status": "will_be_created_on_the_phone", "title": title, "due": due}

    return [
        Tool(
            name="get_current_datetime",
            description="Get the current local date, time and weekday at home.",
            input_schema={"type": "object", "properties": {}},
            handler=get_current_datetime,
        ),
        Tool(
            name="create_reminder",
            description=(
                "Create a reminder on the user's phone (the client app creates it, e.g. the "
                "Siri Shortcut in Apple Reminders), with an optional due date-time (local "
                "time) that triggers a notification."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due": {"type": "string", "description": "Local time, YYYY-MM-DDTHH:MM."},
                    "notes": {"type": "string"},
                },
                "required": ["title"],
            },
            handler=create_reminder,
        ),
    ]
