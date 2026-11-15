"""Where a report the agent wrote by itself goes (a task's result, tasks.py).

A request gets its reply in the HTTP response; a task nobody is waiting on needs a way
out. ``ReportChannel`` is that way: ntfy today (NtfyReports), a chat when one is
configured. To add one, write a class with these four attributes and ``send``, and
register it in ``main.build_delivery`` (and say in ``main.reports_reach`` that it is
configured): the task runner, its tools and the prompt need nothing else.

Every report is also kept in DATA_DIR (Outbox), the last ones only, for a client that
prefers to fetch them: ``GET /v1/tasks/reports``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol

from intelligent_home_agent.notify import NotifyError, Ntfy

log = logging.getLogger(__name__)

KEEP_REPORTS = 50
MAX_KEPT_CHARS = 8000


class DeliveryError(RuntimeError):
    pass


class ReportChannel(Protocol):
    # For the logs and the outbox: "ntfy", "chat".
    name: str
    # How the reply is written (Agent.ask's display): "notification" or "chat".
    display: str
    # The conversation the user answers the report in, when they can: what waits for
    # their go is parked there, and their "yes" runs it. None for a one-way channel: the
    # run then parks nothing.
    conversation_id: str | None
    # How a report reaches the user, in a few words for the model.
    reaches: str

    async def send(self, title: str, text: str) -> None:
        """Raises DeliveryError when the report did not leave."""


class NtfyReports:
    name = "ntfy"
    display = "notification"
    conversation_id = None
    reaches = "a push notification"

    def __init__(self, ntfy: Ntfy, assistant_name: str) -> None:
        self._ntfy = ntfy
        self._assistant = assistant_name

    async def send(self, title: str, text: str) -> None:
        try:
            await self._ntfy.send(f"{self._assistant} · {title}", text)
        except NotifyError as exc:
            raise DeliveryError(str(exc)) from exc


class Outbox:
    """The last reports, newest last, whether or not a channel took them."""

    def __init__(self, file: Path, keep: int = KEEP_REPORTS) -> None:
        self._file = file
        self._keep = keep

    def all(self) -> list[dict[str, str | None]]:
        try:
            reports = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return reports if isinstance(reports, list) else []

    def since(self, at: str | None) -> list[dict[str, str | None]]:
        return [r for r in self.all() if not at or str(r.get("at", "")) > at]

    def put(self, title: str, text: str, at: datetime, sent_by: str | None) -> None:
        report = {
            "at": at.isoformat(timespec="seconds"),
            "title": title,
            "text": text[:MAX_KEPT_CHARS],
            "sent_by": sent_by,
        }
        reports = [*self.all(), report][-self._keep :]
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(json.dumps(reports, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._file)


class Delivery:
    """The channels a report may leave through, the preferred one first."""

    def __init__(
        self, channels: list[ReportChannel] | None = None, outbox: Outbox | None = None
    ) -> None:
        self.channels: list[ReportChannel] = list(channels or [])
        self.outbox = outbox

    @property
    def channel(self) -> ReportChannel | None:
        """The one a task is written for: the first."""
        return self.channels[0] if self.channels else None

    async def deliver(self, title: str, text: str, now: datetime | None = None) -> str | None:
        """Send through the first channel that takes it; the name of that channel, or None
        when none did (the report is then only in the outbox)."""
        sent_by = None
        for channel in self.channels:
            try:
                await channel.send(title, text)
            except DeliveryError as exc:
                log.warning("Report %r not sent through %s: %s", title, channel.name, exc)
                continue
            sent_by = channel.name
            break
        if self.outbox is not None:
            try:
                self.outbox.put(title, text, now or datetime.now().astimezone(), sent_by)
            except OSError as exc:
                log.warning("Report %r not kept: %s", title, exc)
        return sent_by
