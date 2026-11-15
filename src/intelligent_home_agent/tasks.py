"""Tasks the agent starts by itself (docs/tasks.md).

One Markdown file each in the tasks folder (the agent's disk, or a folder of a GitHub
repository), with the front matter of the assistant reminders (``title``, ``on``,
``until``) and the request as body. Every minute the agent reads the folder, and a task
that is due is answered as if the user had written it; the reply goes out through
whatever reaches the user (deliver.py). The task is then moved to ``past/`` with a
``closed:`` line, as a reminder is closed. Which tasks ran is also kept in DATA_DIR, so a
task whose move failed still runs only once.

A repeating task has ``every:`` (``friday``, ``monday, thursday``, ``day``, ``weekday``),
``3 days`` (counted from ``on``) or ``month`` (on ``on``'s day, or the month's last day
when it is shorter). It runs on those days at the time ``on`` gives (09:00 without one),
or at each of ``times``, and stays in the folder until ``until``; each run may stay silent
when it has nothing new to say. Once its last run of the day is over, it is closed for
the day with a ``done:`` line; a ``done:`` the user set for today skips today's runs.

A repeating task with ``digest: yes`` (the morning briefing) gathers the other repeating
tasks due with it: they run first, quietly, and what they found is folded into its one
message instead of a message each. One with ``digest: no`` always gets its own message.

A repeating run carries its last report, written from what an earlier run read, so it
starts tainted and can't use the tools that need a clean request (web_search). One with
``fresh: yes`` (a tech watch) runs instead as a task left once would, clean, never in a
digest: it gets the time of its previous run instead of its last report, to say only what
came out since, and sends nothing when nothing did. A repeating task whose run was refused
such a tool (it asked for a web search) runs that way from then on, unless it says
``fresh: no`` or is a digest, so the user never has to add the line themselves.

The user (and their coding assistant, when the folder is in a repository) writes tasks
there; the agent only through schedule_task, which works in a request that read nothing
else, so nothing it reads (an email, a document) can give it a task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from intelligent_home_agent.i18n import Strings
from intelligent_home_agent.tools.assistant_notes import NoteFolder
from intelligent_home_agent.tools.assistant_reminders import is_due, parse, runs_on, times_of
from intelligent_home_agent.tools.base import ToolError

log = logging.getLogger(__name__)

START_DELAY_SECONDS = 30
# What a repeating task answers when it has nothing new: nothing is sent.
SILENT_REPLY = "NOTHING_NEW"
MAX_REPORT_CHARS = 3000
# The reports before the last one a run also gets: a report that only said what changed
# leaves what still stands to the ones before it.
KEEP_EARLIER_REPORTS = 2
MAX_EARLIER_REPORT_CHARS = 1500
KEEP_RUNS_DAYS = 30


# Kept among the done runs: a repeating task that runs clean since a run was refused a tool.
CLEAN_MARK = "clean:"


def is_fresh(meta: dict[str, str]) -> bool:
    return meta.get("fresh", "").lower() in {"yes", "true"}


def may_run_clean(meta: dict[str, str]) -> bool:
    """Whether a repeating task may switch to clean runs by itself: not a digest, whose
    report is what it is for, nor one the user set to ``fresh: no``."""
    return meta.get("fresh", "").lower() not in {"no", "false"} and meta.get(
        "digest", ""
    ).lower() not in {"yes", "true"}


def since_note(previous: str | None) -> str:
    if previous is None:
        return "\n\n[Runs clean, without your last report: this is its first run.]"
    return (
        f"\n\n[Runs clean, without your last report: its previous run was on {previous}. "
        f"Say only what came out since; when nothing did, answer {SILENT_REPLY}.]"
    )


def says_nothing(text: str) -> bool:
    # The model sometimes puts a sentence before it ("Nothing new on parcels."): still silent.
    return any(line.strip() == SILENT_REPLY for line in text.splitlines())


def occurrence(meta: dict[str, str], now: datetime) -> str | None:
    """The run of a repeating task due now, as "YYYY-MM-DD HH:MM", if it is time.

    ``times: 08:00, 14:00`` runs it at each of those times; a time missed while another
    was due is not run late.
    """
    on = meta.get("on", "")
    try:
        start = date.fromisoformat(on[:10]) if on else None
    except ValueError:  # a date written by hand, wrong: this task never runs, the others do
        return None
    today = now.date()
    until = meta.get("until", "")
    if (start and today < start) or (until and today.isoformat() > until):
        return None
    if meta.get("done") == today.isoformat():
        return None
    passed = [t for t in times_of(meta) if t <= now.strftime("%H:%M")]
    if not runs_on(meta["every"], start, today) or not passed:
        return None
    return f"{today.isoformat()} {max(passed)}"


_TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
MORNING_BRIEFING, EVENING_RECAP = "morning-briefing.md", "evening-recap.md"


def built_in_tasks(s: Strings, morning_at: str | None, evening_at: str | None) -> dict[str, str]:
    """The briefing and the recap as tasks, from the settings (MORNING_BRIEFING_AT,
    EVENING_RECAP_AT): every day at that time, each a digest that folds in the other
    repeating tasks due with it. A file of the same name in the tasks folder replaces one."""
    tasks = {}
    for name, title, request, at in (
        (MORNING_BRIEFING, s.morning_briefing_title, s.briefing_request, morning_at),
        (EVENING_RECAP, s.evening_recap_title, s.recap_request, evening_at),
    ):
        if at and _TIME.match(at.strip()):
            tasks[name] = (
                f"---\ntitle: {title}\non: 2000-01-01 {at.strip().zfill(5)}\nevery: day\n"
                f"digest: yes\n---\n\n{request}\n"
            )
    return tasks


def _save(file: Path, data: object) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(file)


class TaskReports:
    """The last reports sent for each repeating task, so its next run says only what's new:
    the last one, and under ``earlier`` the ones before it, newest first."""

    def __init__(self, file: Path) -> None:
        self._file = file

    def _all(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self._file.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, title: str) -> dict[str, str] | None:
        return self._all().get(title)

    def put(self, title: str, text: str, now: datetime | None = None) -> None:
        reports = self._all()
        at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        report: dict = {"at": at, "text": text[:MAX_REPORT_CHARS]}
        if last := reports.get(title):
            earlier = [
                {"at": r["at"], "text": r["text"][:MAX_EARLIER_REPORT_CHARS]}
                for r in [last, *last.get("earlier", [])]
            ]
            report["earlier"] = earlier[:KEEP_EARLIER_REPORTS]
        reports[title] = report
        _save(self._file, reports)


class TaskRunner:
    def __init__(
        self,
        folder: NoteFolder,
        # (title, body, repeating), and fresh=True for a fresh task's run; True back when a
        # repeating run was refused a tool that clean runs would get.
        start: Callable[..., Awaitable[bool | None]],
        done_file: Path,
        timezone: str,
        poll_seconds: float = 60,
        closer: Callable[[str, str], Awaitable[str]] | None = None,
        day_closer: Callable[[str, str], Awaitable[None]] | None = None,
        # Back: the titles of the gathered tasks that want clean runs, as start says.
        digest: Callable[[str, str, list[tuple[str, str]]], Awaitable[set[str] | None]]
        | None = None,
        # The tasks that come from the settings, by file name (built_in_tasks).
        built_in: dict[str, str] | None = None,
    ) -> None:
        self._folder = folder
        self._built_in = built_in or {}
        self._start = start
        self._done_file = done_file
        self._timezone = timezone
        self._poll_seconds = poll_seconds
        self._close = closer
        self._close_day = day_closer
        self._digest = digest
        self._stored: set[str] = set()

    def _done(self) -> set[str]:
        try:
            return set(json.loads(self._done_file.read_text()))
        except (OSError, ValueError):
            return set()

    def _mark_done(self, name: str, today: date) -> None:
        # A repeating task's runs are kept as "name@YYYY-MM-DD HH:MM", for a month.
        cutoff = (today - timedelta(days=KEEP_RUNS_DAYS)).isoformat()
        done = sorted(
            n for n in self._done() | {name} if "@" not in n or n.rpartition("@")[2] >= cutoff
        )
        _save(self._done_file, done)

    async def check_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(ZoneInfo(self._timezone))
        done = self._done()
        due: list[tuple[str, dict[str, str], str, str]] = []
        files = await self._folder.named_files()
        # Only what is in the folder can be closed there; a task from the settings is not.
        self._stored = set(files)
        for name, text in sorted(({**self._built_in, **files}).items()):
            meta, body = parse(text)
            if not meta.get("title") or not body:
                continue
            if meta.get("every"):
                run = occurrence(meta, now)
                if run is not None and f"{name}@{run}" not in done:
                    due.append((name, meta, body, run))
                continue
            if name in done or not is_due(meta, now):
                continue
            # Marked first: a task that fails is logged, never run twice.
            self._mark_done(name, now.date())
            if self._close and name in self._stored:
                try:
                    await self._close(name, now.strftime("%Y-%m-%d %H:%M"))
                except ToolError as exc:
                    log.warning("Task %s ran but stays in its folder: %s", name, exc)
            log.info("Starting task %s", name)
            await self._start(meta["title"], body, False)
        await self._run_repeating(due, now.date())

    def _previous_run(self, name: str) -> str | None:
        runs = [n.rpartition("@")[2] for n in self._done() if n.startswith(f"{name}@")]
        return max(runs, default=None)

    def _runs_clean(self, name: str, meta: dict[str, str], done: set[str]) -> bool:
        return is_fresh(meta) or (f"{CLEAN_MARK}{name}" in done and may_run_clean(meta))

    def _learn_clean(self, name: str, meta: dict[str, str], today: date) -> None:
        if may_run_clean(meta):
            log.info("Task %s runs clean from now on: a run was refused a tool", name)
            self._mark_done(f"{CLEAN_MARK}{name}", today)

    async def _run_repeating(
        self, due: list[tuple[str, dict[str, str], str, str]], today: date
    ) -> None:
        done = self._done()
        clean = [d for d in due if self._runs_clean(d[0], d[1], done)]
        for name, meta, body, run in clean:
            previous = self._previous_run(name)
            self._mark_done(f"{name}@{run}", today)
            log.info("Starting task %s (%s, clean)", name, run)
            await self._start(meta["title"], body + since_note(previous), False, fresh=True)
            await self._closed_for_day(name, meta, run)
        due = [d for d in due if d not in clean]
        # ``digest: no``: its own message, never folded into the briefing.
        alone = [d for d in due if d[1].get("digest", "").lower() in {"no", "false"}]
        due = [d for d in due if d not in alone]
        digest = next((d for d in due if d[1].get("digest", "").lower() in {"yes", "true"}), None)
        if digest is None or len(due) == 1 or self._digest is None:
            alone, due = alone + due, []
        for name, meta, body, run in alone:
            self._mark_done(f"{name}@{run}", today)
            log.info("Starting task %s (%s)", name, run)
            if await self._start(meta["title"], body, True):
                self._learn_clean(name, meta, today)
            await self._closed_for_day(name, meta, run)
        if not due or digest is None:
            return
        for name, _meta, _body, run in due:
            self._mark_done(f"{name}@{run}", today)
        log.info("Starting digest %s with %d tasks", digest[0], len(due) - 1)
        gathered = [(meta["title"], body) for name, meta, body, _ in due if name != digest[0]]
        wanted = await self._digest(digest[1]["title"], digest[2], gathered) or set()
        for name, meta, _body, _run in due:
            if name != digest[0] and meta["title"] in wanted:
                self._learn_clean(name, meta, today)
        for name, meta, _body, run in due:
            await self._closed_for_day(name, meta, run)

    async def _closed_for_day(self, name: str, meta: dict[str, str], run: str) -> None:
        if self._close_day and name in self._stored and run.endswith(max(times_of(meta))):
            try:
                await self._close_day(name, run[:10])
            except ToolError as exc:
                log.warning("Task %s ran but is not closed for the day: %s", name, exc)

    async def run(self) -> None:
        await asyncio.sleep(START_DELAY_SECONDS)
        while True:
            try:
                await self.check_once()
            except ToolError as exc:
                log.warning("%s", exc)
            except Exception:
                log.exception("Task check failed")
            await asyncio.sleep(self._poll_seconds)
