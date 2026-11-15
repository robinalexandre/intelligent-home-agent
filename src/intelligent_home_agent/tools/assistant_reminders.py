"""Reminders the user left with a coding assistant, one Markdown file each in a GitHub repo.

Any coding agent (or a person) can write them: a file per reminder with a front matter,
then free text. The format is the one of the reminders skill of claude-code-kit
(https://github.com/robinalexandre/claude-code-kit), so a repository synced by the kit
works as it is; nothing here depends on the kit.

    ---
    title: Propose the team meeting
    on: 2026-10-20 09:00        # YYYY-MM-DD or YYYY-MM-DD HH:MM, when it starts showing
    until: 2026-10-24           # last day it shows (optional)
    where: office               # optional: where it applies
    machine: work-laptop        # optional: the computer it is for
    group: Example Corp         # optional: the client or project it belongs to
    ---
    Agenda and invite.

A repeating one adds ``every`` (``day``, ``weekday``, ``monday, thursday``, ``3 days``
counted from ``on``, ``month`` on ``on``'s day), maybe ``times`` (``08:00, 14:00``), and
``done`` (the day it was last closed). Only simple ``key: value`` lines are read; nested
YAML is not. The scheduled tasks (tasks.py) use the same format.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.github import API_URL

log = logging.getLogger(__name__)

MAX_REMINDERS = 20
MAX_BODY_CHARS = 600
DEFAULT_AT = "09:00"
_DAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
_INTERVAL = re.compile(r"^(\d+)\s*days?$")


class ReminderRepo:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        repo: str,
        path: str = "reminders",
        api_url: str = API_URL,
        crypt: ConfigCrypt | None = None,
    ) -> None:
        self._http = http
        self._token = token
        self._base = f"{api_url.rstrip('/')}/repos/{repo}/contents/{path.strip('/')}"
        self._cache: dict[str, tuple[str, str]] = {}
        # The repository's encrypted folders (git-crypt): a file read raw may be in that
        # format. Without a key such a file is refused, never shown as ciphertext.
        self._crypt = crypt

    def _text(self, resp: httpx.Response) -> str:
        data = resp.content
        if self._crypt is None and ConfigCrypt.is_encrypted(data):
            raise ToolError("This file is encrypted (git-crypt) and the agent has no key.")
        if self._crypt is not None:
            try:
                data = self._crypt.decrypt(data)
            except CryptError as exc:
                raise ToolError(f"Encrypted file the agent cannot read: {exc}") from exc
        return data.decode("utf-8", "replace")

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
        return list((await self.named_files()).values())

    async def named_files(self) -> dict[str, str]:
        """By file name; a file is downloaded again only when its content changed."""
        listing = await self._get(self._base, "application/vnd.github+json")
        if listing.status_code == 404:
            return {}
        shas = {
            entry["name"]: entry.get("sha", "")
            for entry in listing.json()
            if entry.get("type") == "file" and entry["name"].endswith(".md")
        }
        stale = [
            name for name, sha in shas.items() if not sha or self._cache.get(name, ("",))[0] != sha
        ]
        raw = await asyncio.gather(
            *(
                self._get(f"{self._base}/{name}", "application/vnd.github.raw+json")
                for name in stale
            )
        )
        for name, resp in zip(stale, raw, strict=True):
            if resp.status_code == 404:
                continue
            try:
                self._cache[name] = (shas[name], self._text(resp))
            except ToolError as exc:
                # One file the agent can't read never hides the others.
                log.warning("%s left out: %s", name, exc)
        return {name: self._cache[name][1] for name in shas if name in self._cache}


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


def runs_on(every: str, start: date | None, day: date) -> bool:
    every = every.strip().lower()
    if every in {"month", "monthly"}:
        if start is None:
            return day.day == 1
        last = calendar.monthrange(day.year, day.month)[1]
        return day.day == min(start.day, last)
    if match := _INTERVAL.match(every):
        step = int(match.group(1))
        return step > 0 and (start is None or (day - start).days % step == 0)
    days: set[int] = set()
    for word in (w.strip() for w in every.split(",")):
        if word in {"day", "daily"}:
            days |= set(range(1, 8))
        elif word in {"weekday", "weekdays"}:
            days |= set(range(1, 6))
        elif word[:3] in _DAYS:
            days.add(_DAYS[word[:3]])
    return day.isoweekday() in days


def times_of(meta: dict[str, str]) -> list[str]:
    """When a repeating one comes each of its days: each of ``times``, else ``on``'s time."""
    on = meta.get("on", "")
    times = [t.strip() for t in meta.get("times", "").split(",") if t.strip()]
    return times or [on[11:16] if len(on) > 10 else DEFAULT_AT]


def is_due(meta: dict[str, str], now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    start = meta.get("on", "")
    until = meta.get("until", "")
    if until and until < today:
        return False
    if meta.get("every"):
        try:
            first = date.fromisoformat(start[:10]) if start else None
        except ValueError:
            return False
        return (
            (first is None or first <= now.date())
            and meta.get("done") != today
            and runs_on(meta["every"], first, now.date())
            and min(times_of(meta)) <= now.strftime("%H:%M")
        )
    if len(start) == 10:
        start += " 00:00"
    return not start or start <= now.strftime("%Y-%m-%d %H:%M")


OPTIONAL_FIELDS = ("every", "where", "machine", "group")


def build_assistant_reminder_tools(repo: ReminderRepo | None, timezone: str) -> list[Tool]:
    if repo is None:
        return []

    async def get_assistant_reminders(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        now = datetime.now(ZoneInfo(timezone))
        due = []
        for name, text in (await repo.named_files()).items():
            meta, body = parse(text)
            if meta.get("title") and is_due(meta, now):
                reminder = {
                    "id": name.removesuffix(".md"),
                    "title": meta["title"],
                    "until": meta.get("until") or None,
                }
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
                "Reminders the user left with their coding assistant that are due today: id, "
                "title, last day they show, repeat days, where and on which machine they "
                "apply, the group they belong to, details."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_assistant_reminders,
            taints=True,
        )
    ]
