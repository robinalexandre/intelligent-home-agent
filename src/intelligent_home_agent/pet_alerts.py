"""The pet's supplies watched: litter, food and the waste bin, told to the user.

Needs the PetKit tools (tools/petkit.py) and a way to reach the user (a chat, or
NTFY_URL). In the daytime, every few hours (PETKIT_CHECK_MINUTES), the PetKit status is read (the same client as the
tools: PetKit allows one session per account). A notification goes out only when something
needs doing:

- the litter under PETKIT_LITTER_ALERT_PERCENT (or the box's own "low" flag): once, again
  only after it went back up;
- the food under PETKIT_FOOD_ALERT_PERCENT when the feeder gives a level, else its own
  "low" flag: the same way;
- the waste bin full: at once, then again every PETKIT_BIN_REMIND_MINUTES until the box
  says it is no longer full.

Nothing is read or sent during the quiet hours (22:00-08:00 by default): the day's first
look comes as they end. A failing read is logged and tried again on the next round.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intelligent_home_agent.i18n import strings

log = logging.getLogger(__name__)

# How often the quiet hours are looked at, to start the day's first check soon after.
QUIET_POLL_SECONDS = 600
# How far above the threshold a level must come back before it can be alerted again.
REARM_MARGIN = 5

Status = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[str], Awaitable[Any]]


class PetAlerts:
    def __init__(
        self,
        status: Status,
        file: Path,
        timezone: str,
        *,
        language: str = "en",
        litter_percent: int = 50,
        food_percent: int = 45,
        remind_minutes: int = 120,
        check_minutes: int = 180,
        quiet: tuple[int, int] = (22, 8),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._status = status
        self._file = file
        self._tz = ZoneInfo(timezone)
        self._s = strings(language)
        self._litter = litter_percent
        self._food = food_percent
        self._remind = remind_minutes * 60
        self.check_seconds = check_minutes * 60
        self._quiet = quiet
        self._clock = clock
        self.state: dict[str, Any] = {"low": {}, "bins": {}}
        try:
            saved = json.loads(file.read_text())
            if isinstance(saved, dict):
                self.state.update(saved)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False))
        tmp.replace(self._file)

    def quiet_now(self) -> bool:
        hour = self._clock().astimezone(self._tz).hour
        start, end = self._quiet
        return hour >= start or hour < end if start > end else start <= hour < end

    async def check(self, send: Send) -> list[str]:
        """One look; what was said. Nothing is said during the quiet hours."""
        s = self._s
        status = await self._status()
        said: list[str] = []
        quiet = self.quiet_now()
        for box in status.get("litter_boxes", []):
            name = str(box.get("name") or s.pet_litter_name)
            level = box.get("litter_percent")
            low = (level is not None and level < self._litter) or bool(box.get("litter_low"))
            text = s.pet_litter_low.format(name=name, level=level)
            if level is None:
                text = s.pet_litter_low_no_level.format(name=name)
            said += await self._level(f"litter:{name}", low, level, self._litter, text, send, quiet)
            said += await self._bin(name, bool(box.get("waste_bin_full")), send, quiet)
        for feeder in status.get("feeders", []):
            name = str(feeder.get("name") or s.pet_feeder_name)
            level = feeder.get("food_left_percent")
            low = level < self._food if level is not None else bool(feeder.get("food_low"))
            text = s.pet_food_low.format(name=name, level=level)
            if level is None:
                text = s.pet_food_low_no_level.format(name=name)
            said += await self._level(f"food:{name}", low, level, self._food, text, send, quiet)
        self.save()
        return said

    async def _level(
        self,
        key: str,
        low: bool,
        level: int | None,
        threshold: int,
        text: str,
        send: Send,
        quiet: bool,
    ) -> list[str]:
        alerted = self.state["low"].get(key, False)
        if low and not alerted:
            if quiet:
                return []
            await send(text)
            self.state["low"][key] = True
            return [text]
        back_up = level >= threshold + REARM_MARGIN if level is not None else not low
        if alerted and not low and back_up:
            self.state["low"][key] = False
        return []

    async def _bin(self, name: str, full: bool, send: Send, quiet: bool) -> list[str]:
        bins = self.state["bins"]
        seen = bins.get(name) or {}
        if not full:
            if seen:
                bins.pop(name, None)
            return []
        if quiet:
            return []
        now = self._clock().timestamp()
        if seen.get("last") and now - seen["last"] < self._remind:
            return []
        again = self._s.pet_bin_again if seen.get("last") else ""
        text = self._s.pet_bin_full.format(name=name, again=again)
        # The reminders stop when the box says it is no longer full: the message itself
        # takes no answer.
        await send(text)
        bins[name] = {"last": now}
        return [text]

    async def watch(self, send: Send) -> None:
        """A look every check_seconds in the daytime, the first one as the quiet hours end;
        no PetKit read at night."""
        while True:
            if self.quiet_now():
                await asyncio.sleep(QUIET_POLL_SECONDS)
                continue
            try:
                await self.check(send)
            except Exception:
                log.warning("Pet supplies check failed", exc_info=True)
            await asyncio.sleep(self.check_seconds)
