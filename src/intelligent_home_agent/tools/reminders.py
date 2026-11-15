"""The user's Apple Reminders, read and written through iCloud's web services (icloud_web).

The Reminders zone is read whole once from its change feed, then only what changed since,
so a call stays a few requests with hundreds of reminders. A query by list would need an
index Apple builds on first use, and it answers TRY_AGAIN_LATER meanwhile.
Marking a repeating reminder done moves it to its next day, as the iPhone does, instead of
ending it.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.calendar import format_when

if TYPE_CHECKING:
    from intelligent_home_agent.icloud_web import ICloudWeb

MAX_REMINDERS = 50
MAX_TITLE_CHARS = 300
# A relative due time ("in 2 minutes") up to a week; further, a date reads better.
MAX_IN_MINUTES = 7 * 24 * 60
# pyicloud's RecurrenceFrequency, as Apple writes it.
DAILY, WEEKLY, MONTHLY, YEARLY = range(4)
# A rule record holding only these is "every N days, weeks, months or years".
_PLAIN_RULE_FIELDS = {
    "Deleted",
    "FirstDayOfTheWeek",
    "Frequency",
    "Imported",
    "Interval",
    "OccurrenceCount",
    "Reminder",
    "ResolutionTokenMap",
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reminder:
    id: str
    title: str
    due: datetime | date | None
    list_name: str
    notes: str | None = None
    flagged: bool = False
    repeats: bool = False


class ReminderSource(Protocol):
    async def open_reminders(self) -> list[Reminder]: ...

    async def list_names(self) -> list[str]: ...

    async def find(self, reminder_id: str) -> Reminder | None: ...

    async def create(
        self, title: str, list_name: str | None, due: datetime | date | None, notes: str
    ) -> Reminder: ...

    async def update(
        self,
        reminder_id: str,
        title: str | None,
        due: datetime | date | None,
        notes: str | None,
    ) -> Reminder: ...

    async def complete(self, reminder_id: str) -> Reminder | None: ...


def next_due(due: datetime | date, frequency: int, interval: int, after: date) -> datetime | date:
    """The first occurrence of the rule after the day `after`, from `due` on."""
    step = max(interval, 1)
    count = 1
    while True:
        if frequency in (DAILY, WEEKLY):
            days = step * count * (7 if frequency == WEEKLY else 1)
            moved = due + timedelta(days=days)
        else:
            months = step * count * (12 if frequency == YEARLY else 1)
            year, month = divmod(due.month - 1 + months, 12)
            year, month = due.year + year, month + 1
            day = min(due.day, calendar.monthrange(year, month)[1])
            moved = due.replace(year=year, month=month, day=day)
        day_of = moved.date() if isinstance(moved, datetime) else moved
        if day_of > after:
            return moved
        count += 1


def plain_rule(api: Any, rule_ids: list[str]) -> tuple[int, int] | None:
    """(frequency, interval) of a reminder's only rule when it is "every N days, weeks,
    months or years", from the raw record: pyicloud's model drops the rest (set days, the
    month's last day, an end), which a step from the due date would get wrong."""
    if len(rule_ids) != 1:
        return None
    try:
        from pyicloud.services.reminders._protocol import _as_record_name
        from pyicloud.services.reminders._reads import _REMINDERS_ZONE_REQ

        raw = api.reminders._reads._get_raw()
        resp = raw.lookup(
            record_names=[_as_record_name(rule_ids[0], "RecurrenceRule")],
            zone_id=_REMINDERS_ZONE_REQ,
        )
        fields = resp.records[0].fields
        items = fields.root.items() if hasattr(fields, "root") else fields.items()
        values = {name: getattr(f, "value", f) for name, f in items}
    except Exception:
        log.warning("could not read a recurrence rule", exc_info=True)
        return None
    if set(values) - _PLAIN_RULE_FIELDS or values.get("OccurrenceCount"):
        return None
    frequency = int(values.get("Frequency") or 0)
    if frequency not in (DAILY, WEEKLY, MONTHLY, YEARLY):
        return None
    return frequency, int(values.get("Interval") or 1)


class ICloudReminders:
    def __init__(self, icloud: ICloudWeb, list_names: list[str], default_list: str | None) -> None:
        self._icloud = icloud
        # Only these lists (by name); empty means all of them.
        self._names = {name.casefold() for name in list_names}
        self._default = default_list
        self._items: dict[str, Any] = {}
        self._lists: dict[str, str] = {}
        self._cursor: str | None = None

    def _sync(self, api: Any) -> None:
        service = api.reminders
        cursor = service.sync_cursor()
        for event in service.iter_changes(since=self._cursor):
            if event.type == "deleted" or event.reminder is None or event.reminder.deleted:
                self._items.pop(event.reminder_id, None)
            else:
                self._items[event.reminder_id] = event.reminder
        self._cursor = cursor
        if not self._lists or any(r.list_id not in self._lists for r in self._items.values()):
            self._lists = {lst.id: lst.title for lst in service.lists() if not lst.deleted}

    def _shown(self, list_id: str) -> bool:
        name = self._lists.get(list_id, "")
        return not self._names or name.casefold() in self._names

    def _reminder(self, item: Any) -> Reminder:
        due: datetime | date | None = item.due_date
        if due is not None and item.all_day:
            due = due.astimezone(UTC).date() if due.tzinfo else due.date()
        return Reminder(
            id=item.id,
            title=" ".join((item.title or "").split()) or "(untitled)",
            due=due,
            list_name=self._lists.get(item.list_id, ""),
            notes=(item.desc or "").strip() or None,
            flagged=bool(item.flagged),
            repeats=bool(item.recurrence_rule_ids),
        )

    def _item(self, api: Any, reminder_id: str) -> Any:
        self._sync(api)
        item = self._items.get(reminder_id)
        if item is None or not self._shown(item.list_id):
            raise ToolError(f"No reminder {reminder_id}: get_reminders gives the ids.")
        return api.reminders.get(reminder_id)

    def _list_id(self, name: str | None) -> str:
        wanted = name or self._default
        shown = {i: n for i, n in self._lists.items() if self._shown(i)}
        if wanted:
            for list_id, title in shown.items():
                if title.casefold() == wanted.casefold():
                    return list_id
            raise ToolError(f"No list {wanted!r}. Lists: {', '.join(sorted(shown.values()))}.")
        if not shown:
            raise ToolError("No Reminders list to add to.")
        # No default set: the list that holds the most open reminders.
        open_counts = {i: 0 for i in shown}
        for item in self._items.values():
            if not item.completed and item.list_id in open_counts:
                open_counts[item.list_id] += 1
        return max(open_counts, key=open_counts.__getitem__)

    @staticmethod
    def _set_due(item: Any, due: datetime | date | None) -> None:
        if isinstance(due, datetime):
            item.due_date, item.all_day = due, False
            item.time_zone = getattr(due.tzinfo, "key", None)
        elif isinstance(due, date):
            # All-day dates float: midnight UTC, without a zone, as the iPhone writes them.
            item.due_date, item.all_day, item.time_zone = (
                datetime.combine(due, time(), UTC),
                True,
                None,
            )

    async def open_reminders(self) -> list[Reminder]:
        def fetch(api: Any) -> list[Reminder]:
            self._sync(api)
            return [
                self._reminder(item)
                for item in self._items.values()
                if not item.completed and self._shown(item.list_id)
            ]

        return await self._icloud.run(fetch)

    async def list_names(self) -> list[str]:
        def names(api: Any) -> list[str]:
            self._sync(api)
            return sorted(n for i, n in self._lists.items() if self._shown(i))

        return await self._icloud.run(names)

    async def find(self, reminder_id: str) -> Reminder | None:
        """From what was read last, without asking iCloud: for showing a write."""
        item = self._items.get(reminder_id)
        return self._reminder(item) if item is not None else None

    async def create(
        self, title: str, list_name: str | None, due: datetime | date | None, notes: str
    ) -> Reminder:
        def add(api: Any) -> Reminder:
            self._sync(api)
            list_id = self._list_id(list_name)
            all_day = isinstance(due, date) and not isinstance(due, datetime)
            moment = datetime.combine(due, time(), UTC) if all_day else due
            item = api.reminders.create(
                list_id=list_id,
                title=title,
                desc=notes,
                due_date=moment,
                all_day=all_day,
                time_zone=getattr(getattr(due, "tzinfo", None), "key", None),
            )
            self._items[item.id] = item
            return self._reminder(item)

        return await self._icloud.run(add)

    async def update(
        self,
        reminder_id: str,
        title: str | None,
        due: datetime | date | None,
        notes: str | None,
    ) -> Reminder:
        def change(api: Any) -> Reminder:
            item = self._item(api, reminder_id)
            if title is not None:
                item.title = title
            if notes is not None:
                item.desc = notes
            self._set_due(item, due)
            api.reminders.update(item)
            self._items[item.id] = item
            return self._reminder(item)

        return await self._icloud.run(change)

    async def complete(self, reminder_id: str) -> Reminder | None:
        """Done; a repeating one comes back as its next occurrence, which is returned."""

        def done(api: Any) -> Reminder | None:
            item = self._item(api, reminder_id)
            if item.recurrence_rule_ids:
                rule = plain_rule(api, item.recurrence_rule_ids)
                if rule is None or item.due_date is None:
                    raise ToolError(
                        "This reminder repeats on a rule the agent can't follow (set days, the "
                        "month's last day, an end...): ticking it here could end it or skip a "
                        "date. Tell the user to tick it on their iPhone."
                    )
                zone = ZoneInfo(item.time_zone or "UTC")
                current = self._reminder(item).due
                assert current is not None
                if isinstance(current, datetime):
                    # Stepped in its own zone, so 9:00 stays 9:00 across a DST change.
                    current = current.astimezone(zone)
                today = datetime.now(zone).date()
                self._set_due(item, next_due(current, *rule, today))
                api.reminders.update(item)
                self._items[item.id] = item
                return self._reminder(item)
            item.completed = True
            api.reminders.update(item)
            self._items[item.id] = item
            return None

        return await self._icloud.run(done)


def _due_moment(reminder: Reminder, tz: ZoneInfo) -> datetime | None:
    if reminder.due is None:
        return None
    due = reminder.due
    if isinstance(due, datetime):
        return due.astimezone(tz) if due.tzinfo else due.replace(tzinfo=tz)
    return datetime.combine(due, time.max, tz)


def _parse_due(value: Any, tz: ZoneInfo) -> datetime | date:
    text = str(value or "").strip()
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError('due is "YYYY-MM-DD HH:MM" (local time) or "YYYY-MM-DD".') from exc
    # In the home's zone by name: Apple shows a due time without one as floating.
    return moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=tz)


def _due_from(args: dict[str, Any], tz: ZoneInfo) -> datetime | date | None:
    """due, or in_minutes from the moment the reminder is written: the request's time in
    the prompt is already minutes old when a slow request gets there."""
    if args.get("in_minutes") is not None:
        try:
            minutes = int(args["in_minutes"])
        except (TypeError, ValueError) as exc:
            raise ToolError("in_minutes is a whole number of minutes.") from exc
        if not 0 < minutes <= MAX_IN_MINUTES:
            raise ToolError(f"in_minutes is between 1 and {MAX_IN_MINUTES}; use due beyond.")
        moment = datetime.now(tz) + timedelta(minutes=minutes)
        # Rounded up to the minute: Reminders shows minutes, and never a time just gone.
        return moment.replace(second=0, microsecond=0) + timedelta(
            minutes=1 if moment.second or moment.microsecond else 0
        )
    return _parse_due(args["due"], tz) if args.get("due") else None


def _title(value: Any) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise ToolError("A reminder needs a title.")
    return title[:MAX_TITLE_CHARS]


def build_reminder_tools(
    source: ReminderSource, timezone: str, language: str = "en"
) -> list[Tool]:
    tz = ZoneInfo(timezone)
    s = strings(language)

    def shown(r: Reminder, moment: datetime | None, now: datetime) -> dict[str, Any]:
        return {
            "id": r.id,
            "title": r.title,
            "due": format_when(r.due, tz),
            "overdue": moment is not None and moment < now,
            "list": r.list_name,
            "notes": r.notes,
            "flagged": r.flagged,
            "repeats": r.repeats,
        }

    async def get_reminders(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        until: date | None = None
        if args.get("until"):
            try:
                until = date.fromisoformat(str(args["until"]))
            except ValueError as exc:
                raise ToolError("until must be YYYY-MM-DD") from exc
        wanted = str(args.get("list") or "").casefold()
        now = datetime.now(tz)
        dated = []
        undated = []
        for reminder in await source.open_reminders():
            if wanted and reminder.list_name.casefold() != wanted:
                continue
            moment = _due_moment(reminder, tz)
            if moment is None:
                undated.append(reminder)
            elif until is None or moment.date() <= until:
                dated.append((moment, reminder))
        dated.sort(key=lambda pair: pair[0])
        undated.sort(key=lambda r: (r.list_name.casefold(), r.title.casefold()))
        chosen = [(moment, r) for moment, r in dated]
        if until is None:
            chosen += [(None, r) for r in undated]
        return {
            "until": until.isoformat() if until else None,
            "count": len(chosen),
            "lists": await source.list_names(),
            "reminders": [shown(r, moment, now) for moment, r in chosen[:MAX_REMINDERS]],
        }

    async def create_reminder(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        due = _due_from(args, tz)
        reminder = await source.create(
            _title(args.get("title")), args.get("list"), due, str(args.get("notes") or "")
        )
        return {"created": shown(reminder, _due_moment(reminder, tz), datetime.now(tz))}

    async def update_reminder(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        reminder = await source.update(
            str(args.get("id") or ""),
            _title(args["title"]) if args.get("title") else None,
            _due_from(args, tz),
            str(args["notes"]) if args.get("notes") is not None else None,
        )
        return {"updated": shown(reminder, _due_moment(reminder, tz), datetime.now(tz))}

    async def complete_reminder(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        following = await source.complete(str(args.get("id") or ""))
        if following is None:
            return {"status": "done"}
        return {"status": "done, it repeats", "next_due": format_when(following.due, tz)}

    async def named(args: dict[str, Any]) -> str:
        found = await source.find(str(args.get("id") or ""))
        if found is None:
            raise ToolError(f"No reminder {args.get('id')}: get_reminders gives the ids.")
        return s.reminder_named.format(title=found.title, list=found.list_name)

    def when_text(args: dict[str, Any]) -> str:
        if args.get("in_minutes") is not None:
            return s.reminder_in_minutes.format(minutes=int(args["in_minutes"]))
        if not args.get("due"):
            return ""
        return s.reminder_due.format(when=format_when(_parse_due(args["due"], tz), tz))

    async def describe_create(args: dict[str, Any]) -> str:
        when = when_text(args)
        return s.reminder_create_summary.format(
            title=_title(args.get("title")),
            where=s.reminder_in_list.format(list=args["list"]) if args.get("list") else "",
            when=f", {when}" if when else "",
            notes=s.calendar_notes.format(notes=args["notes"]) if args.get("notes") else "",
        )

    async def describe_update(args: dict[str, Any]) -> str:
        changes = []
        if args.get("title"):
            changes.append(s.reminder_change_title.format(title=_title(args["title"])))
        if when := when_text(args):
            changes.append(when)
        if args.get("notes") is not None:
            changes.append(s.reminder_change_notes.format(notes=args["notes"]))
        return s.reminder_update_summary.format(
            reminder=await named(args), what=", ".join(changes) or s.draft_no_change
        )

    async def describe_complete(args: dict[str, Any]) -> str:
        return s.reminder_complete_summary.format(reminder=await named(args))

    id_schema = {"type": "string", "description": "The id get_reminders gave."}
    due_schema = {
        "type": "string",
        "description": '"YYYY-MM-DD HH:MM" local time, or "YYYY-MM-DD".',
    }
    in_minutes_schema = {
        "type": "integer",
        "description": (
            'Instead of due, for "in 2 minutes", "in an hour": counted from when the '
            "reminder is written, not from the time in your prompt, which may be minutes old."
        ),
    }
    return [
        Tool(
            name="get_reminders",
            description=(
                "The user's open Apple Reminders (iCloud): id, title, due date, list, notes, "
                "whether it repeats, and the names of their lists. Without until: all of "
                "them, undated ones last. With until (YYYY-MM-DD): only those due by that "
                "day, overdue first. list keeps one list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "until": {"type": "string", "description": "Last due day, YYYY-MM-DD."},
                    "list": {"type": "string", "description": "Only this list, by name."},
                },
            },
            handler=get_reminders,
            taints=True,
        ),
        Tool(
            name="create_reminder",
            description=(
                "Add a reminder to the user's Apple Reminders (iCloud), on all their devices. "
                "With a due time, their iPhone notifies them then. Without list: their usual "
                "list. After reading anything, it waits for their go."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due": due_schema,
                    "in_minutes": in_minutes_schema,
                    "list": {"type": "string", "description": "One of the user's lists, by name."},
                    "notes": {"type": "string"},
                },
                "required": ["title"],
            },
            handler=create_reminder,
            needs_clean_turn=True,
            proposable=True,
            describe=describe_create,
            multiline_args=frozenset({"notes"}),
        ),
        Tool(
            name="update_reminder",
            description=(
                "Change one of the user's Apple Reminders, by its id from get_reminders: a new "
                "title, due date (to postpone it) or notes; what isn't given stays."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": id_schema,
                    "title": {"type": "string"},
                    "due": due_schema,
                    "in_minutes": in_minutes_schema,
                    "notes": {"type": "string"},
                },
                "required": ["id"],
            },
            handler=update_reminder,
            needs_clean_turn=True,
            proposable=True,
            describe=describe_update,
            multiline_args=frozenset({"notes"}),
        ),
        Tool(
            name="complete_reminder",
            description=(
                "Mark one of the user's Apple Reminders done (checked), by its id from "
                "get_reminders, when they say it is done. A repeating one moves to its next "
                "date, as on the iPhone."
            ),
            input_schema={
                "type": "object",
                "properties": {"id": id_schema},
                "required": ["id"],
            },
            handler=complete_reminder,
            needs_clean_turn=True,
            proposable=True,
            describe=describe_complete,
        ),
    ]
