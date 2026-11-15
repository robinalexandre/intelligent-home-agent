"""News of the sub-tasks that run in the background for a request of the user's own.

A coding job goes on after the reply left. Each one gets an estimate from how long the
last ones of its kind took (its default until there are some), said when it starts; then,
about each quarter of that time, the user hears in one line where every running one
stands, through the channel the reports leave by (deliver.py). Tasks the user left earlier
and the code's own runs (the nightly review) are not tracked: nobody is waiting for them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from intelligent_home_agent.i18n import strings

log = logging.getLogger(__name__)

KEPT_DURATIONS = 10
TICK_SECONDS = 30.0
# One line at most this often, however many sub-tasks cross a quarter.
MIN_GAP_SECONDS = 120.0


@dataclass
class Run:
    kind: str
    label: str
    estimate: float
    # How far it got, in a few words (what the coder last said).
    detail: Callable[[], str] | None = None
    started: float | None = None
    quarters: int = 0
    done: bool = False
    _tracker: Any = field(default=None, repr=False)

    @property
    def estimate_minutes(self) -> int:
        return max(1, round(self.estimate / 60))

    def go(self) -> None:
        """It stops waiting (for a slot, for the reviews it compares) and starts."""
        if self.started is None:
            self.started = self._tracker.now()

    def end(self, ok: bool = True) -> None:
        if not self.done:
            self.done = True
            self._tracker.ended(self, ok)


class Tracker:
    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        durations_file: Path,
        language: str = "en",
        *,
        tick: float = TICK_SECONDS,
        min_gap: float = MIN_GAP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._file = durations_file
        self._texts = strings(language)
        self._tick = tick
        self._min_gap = min_gap
        self.now = clock
        self._runs: list[Run] = []
        self._last_sent = float("-inf")

    def _durations(self) -> dict[str, list[float]]:
        try:
            return json.loads(self._file.read_text())
        except (OSError, ValueError):
            return {}

    def estimate(self, kind: str, default: float) -> float:
        past = self._durations().get(kind) or []
        return statistics.median(past) if past else default

    def begin(
        self,
        kind: str,
        label: str,
        default_seconds: float,
        detail: Callable[[], str] | None = None,
        *,
        waiting: bool = False,
    ) -> Run:
        run = Run(kind, label, self.estimate(kind, default_seconds), detail, _tracker=self)
        if not waiting:
            run.go()
        self._runs.append(run)
        return run

    def ended(self, run: Run, ok: bool) -> None:
        self._runs = [r for r in self._runs if r is not run]
        if not ok or run.started is None:
            return
        durations = self._durations()
        kept = [*durations.get(run.kind, []), self.now() - run.started][-KEPT_DURATIONS:]
        durations[run.kind] = kept
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_name(self._file.name + ".tmp")
            tmp.write_text(json.dumps(durations))
            os.chmod(tmp, 0o600)
            tmp.replace(self._file)
        except OSError:
            log.warning("Could not keep the durations of %s", run.kind, exc_info=True)

    def line(self) -> str:
        """Where every running sub-task stands, in one line."""
        parts = []
        for run in self._runs:
            if run.started is None:
                state = self._texts.progress_waiting
            else:
                elapsed = self.now() - run.started
                minutes = round(elapsed / 60)
                if elapsed > run.estimate:
                    state = self._texts.progress_late.format(minutes=minutes)
                else:
                    percent = min(95, round(elapsed / run.estimate * 100 / 5) * 5)
                    state = self._texts.progress_share.format(
                        percent=percent, minutes=minutes, estimate=run.estimate_minutes
                    )
                if run.detail is not None and (detail := run.detail()):
                    state += f", {detail}"
            parts.append(self._texts.progress_line.format(label=run.label, state=state))
        return "⏳ " + " · ".join(parts)

    def _crossed(self) -> list[Run]:
        """The runs that reached a new quarter of their estimate since the last line
        (past it, each half of it more)."""
        crossed = []
        for run in self._runs:
            if run.started is None:
                continue
            elapsed = self.now() - run.started
            ratio = elapsed / run.estimate
            quarters = int(ratio * 4) if ratio <= 1 else 4 + int((ratio - 1) * 2)
            if quarters > run.quarters:
                run.quarters = quarters
                crossed.append(run)
        return crossed

    async def check(self) -> None:
        if self.now() - self._last_sent < self._min_gap:
            return
        if self._crossed():
            self._last_sent = self.now()
            try:
                await self._send(self.line())
            except Exception:
                log.warning("Could not send the sub-tasks' news", exc_info=True)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            await self.check()
