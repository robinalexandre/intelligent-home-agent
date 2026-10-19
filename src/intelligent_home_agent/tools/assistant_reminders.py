"""Reminders the user left with a coding assistant, one Markdown file each in a GitHub repo.

Any coding agent (or a person) can write them: a file per reminder with a YAML front matter,
then free text.

    ---
    title: Propose the team meeting
    on: 2026-10-20 09:00        # YYYY-MM-DD or YYYY-MM-DD HH:MM, when it starts showing
    until: 2026-10-24           # last day it shows (optional)
    where: office               # optional: where it applies
    machine: work-laptop        # optional: the computer it was written on
    ---
    Agenda and invite.

Only simple ``key: value`` lines are read; nested YAML is not.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.github import API_URL

MAX_REMINDERS = 20
MAX_BODY_CHARS = 600
OPTIONAL_FIELDS = ("where", "machine")


class ReminderRepo:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        repo: str,
        path: str = "reminders",
        api_url: str = API_URL,
    ) -> None:
        self._http = http
        self._token = token
        self._base = f"{api_url.rstrip('/')}/repos/{repo}/contents/{path.strip('/')}"

    async def _get(self, url: str, accept: str) -> httpx.Response:
        try:
            resp = await self._http.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": accept,
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if resp.status_code == 404:
                return resp
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"Reminders unavailable: {exc}") from exc
        return resp

    async def files(self) -> list[str]:
        """The Markdown files of the reminders folder, as raw text."""
        listing = await self._get(self._base, "application/vnd.github+json")
        if listing.status_code == 404:
            return []
        names = [
            entry["name"]
            for entry in listing.json()
            if entry.get("type") == "file" and entry["name"].endswith(".md")
        ]
        raw = await asyncio.gather(
            *(
                self._get(f"{self._base}/{name}", "application/vnd.github.raw+json")
                for name in names
            )
        )
        return [resp.text for resp in raw if resp.status_code != 404]


def parse(text: str) -> tuple[dict[str, str], str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :]).strip()
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return {}, text.strip()


def is_due(meta: dict[str, str], now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    start = meta.get("on", "")
    if len(start) == 10:
        start += " 00:00"
    until = meta.get("until", "")
    return (not start or start <= now.strftime("%Y-%m-%d %H:%M")) and (not until or until >= today)


def build_assistant_reminder_tools(repo: ReminderRepo | None, timezone: str) -> list[Tool]:
    if repo is None:
        return []

    async def get_assistant_reminders(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        now = datetime.now(ZoneInfo(timezone))
        due = []
        for text in await repo.files():
            meta, body = parse(text)
            if meta.get("title") and is_due(meta, now):
                reminder = {"title": meta["title"], "until": meta.get("until") or None}
                for key in OPTIONAL_FIELDS:
                    if meta.get(key):
                        reminder[key] = meta[key]
                reminder["details"] = body[:MAX_BODY_CHARS]
                due.append(reminder)
        return {"count": len(due), "reminders": due[:MAX_REMINDERS]}

    return [
        Tool(
            name="get_assistant_reminders",
            description=(
                "Reminders the user left with their coding assistant that are due today: "
                "title, last day they show, where and on which machine they apply, details."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_assistant_reminders,
            taints=True,
        )
    ]
