from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from intelligent_home_agent.icloud_web import RENEW, ICloudWeb
from intelligent_home_agent.tools import ToolError, build_reminder_tools
from intelligent_home_agent.tools.reminders import (
    DAILY,
    MONTHLY,
    WEEKLY,
    YEARLY,
    ICloudReminders,
    Reminder,
    next_due,
)

ROME = ZoneInfo("Europe/Rome")


class FakeReminders:
    def __init__(self, reminders):
        self._reminders = reminders
        self.calls = 0

    async def open_reminders(self):
        self.calls += 1
        return self._reminders

    async def list_names(self):
        return sorted({r.list_name for r in self._reminders})

    async def find(self, reminder_id):
        return next((r for r in self._reminders if r.id == reminder_id), None)


REMINDERS = [
    Reminder("r1", "Yoghurt", None, "Shopping"),
    Reminder("r2", "Holidays", date(2099, 7, 1), "General"),
    Reminder("r3", "Taxes", datetime(2020, 5, 2, 10, 0, tzinfo=ROME), "General"),
    Reminder("r4", "Call Jo", datetime(2099, 1, 1, 7, 0, tzinfo=UTC), "General"),
    Reminder("r5", "Water", None, "Shopping"),
]


def tool(tools, name):
    return next(t for t in tools if t.name == name)


async def test_reminders_due_first_then_undated(ctx):
    get = tool(build_reminder_tools(FakeReminders(REMINDERS), "Europe/Rome"), "get_reminders")

    result = await get.handler({}, ctx)

    assert [r["title"] for r in result["reminders"]] == [
        "Taxes",
        "Call Jo",
        "Holidays",
        "Water",
        "Yoghurt",
    ]
    assert result["count"] == 5 and result["until"] is None
    assert result["lists"] == ["General", "Shopping"]
    assert result["reminders"][0]["overdue"] is True
    assert result["reminders"][1] == {
        "id": "r4",
        "title": "Call Jo",
        "due": "2099-01-01T08:00+01:00",
        "overdue": False,
        "list": "General",
        "notes": None,
        "flagged": False,
        "repeats": False,
    }
    assert result["reminders"][3]["due"] is None
    assert get.taints


async def test_until_keeps_overdue_and_due_by_that_day_only(ctx):
    get = tool(build_reminder_tools(FakeReminders(REMINDERS), "Europe/Rome"), "get_reminders")

    result = await get.handler({"until": "2099-01-01"}, ctx)

    assert [r["title"] for r in result["reminders"]] == ["Taxes", "Call Jo"]
    assert result["until"] == "2099-01-01" and result["count"] == 2


async def test_one_list_only(ctx):
    get = tool(build_reminder_tools(FakeReminders(REMINDERS), "Europe/Rome"), "get_reminders")

    result = await get.handler({"list": "shopping"}, ctx)

    assert [r["title"] for r in result["reminders"]] == ["Water", "Yoghurt"]


async def test_bad_until_is_reported_to_the_model(ctx):
    get = tool(build_reminder_tools(FakeReminders([]), "Europe/Rome"), "get_reminders")
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await get.handler({"until": "tomorrow"}, ctx)


async def test_writes_wait_after_a_read_and_show_what_they_do():
    tools = build_reminder_tools(FakeReminders(REMINDERS), "Europe/Rome")
    for name in ("create_reminder", "update_reminder", "complete_reminder"):
        assert tool(tools, name).needs_clean_turn and tool(tools, name).proposable

    create = tool(tools, "create_reminder")
    line = await create.summary({"title": "Oil change", "due": "2026-11-03 09:00", "list": "TO DO"})
    assert line == 'Create the reminder "Oil change" in TO DO, due 2026-11-03T09:00+01:00'
    done = await tool(tools, "complete_reminder").summary({"id": "r3"})
    assert done == 'Tick the reminder "Taxes" (General)'
    french = build_reminder_tools(FakeReminders(REMINDERS), "Europe/Rome", "fr")
    done = await tool(french, "complete_reminder").summary({"id": "r3"})
    assert done == "Cocher le rappel « Taxes » (General)"
    change = await tool(tools, "update_reminder").summary({"id": "r3", "notes": "by post"})
    assert change == 'Change the reminder "Taxes" (General): notes: by post'
    with pytest.raises(ToolError, match="get_reminders"):
        await tool(tools, "complete_reminder").summary({"id": "nope"})


def test_next_due_steps_past_today():
    nine = datetime(2026, 10, 20, 9, 0, tzinfo=ROME)
    assert next_due(nine, WEEKLY, 1, date(2026, 10, 24)) == datetime(
        2026, 10, 27, 9, 0, tzinfo=ROME
    )
    assert next_due(nine, DAILY, 3, date(2026, 10, 20)) == datetime(2026, 10, 23, 9, 0, tzinfo=ROME)
    # The 31st falls on each month's last day, without drifting.
    assert next_due(date(2026, 1, 31), MONTHLY, 1, date(2026, 1, 31)) == date(2026, 2, 28)
    assert next_due(date(2026, 1, 31), MONTHLY, 1, date(2026, 3, 1)) == date(2026, 3, 31)
    assert next_due(date(2024, 2, 29), YEARLY, 1, date(2024, 3, 1)) == date(2025, 2, 28)


@dataclass
class Item:
    id: str
    title: str
    list_id: str = "List/todo"
    desc: str = ""
    completed: bool = False
    due_date: datetime | None = None
    all_day: bool = False
    deleted: bool = False
    flagged: bool = False
    time_zone: str | None = None
    recurrence_rule_ids: list[str] = field(default_factory=list)


class FakeRaw:
    def __init__(self, rules):
        self.rules = rules

    def lookup(self, record_names, zone_id):
        fields = {k: SimpleNamespace(value=v) for k, v in self.rules[record_names[0]].items()}
        return SimpleNamespace(records=[SimpleNamespace(fields=fields)])


class FakeService:
    def __init__(self, items, rules=None):
        self.items = {i.id: i for i in items}
        self.feed = [SimpleNamespace(type="updated", reminder_id=i.id, reminder=i) for i in items]
        self.cursors = []
        self.updated = []
        self.created = []
        self._reads = SimpleNamespace(_get_raw=lambda: FakeRaw(rules or {}))

    def sync_cursor(self):
        return f"c{len(self.cursors) + 1}"

    def iter_changes(self, since=None):
        self.cursors.append(since)
        events, self.feed = self.feed, []
        return events

    def lists(self):
        return [
            SimpleNamespace(id="List/todo", title="TO DO", deleted=False),
            SimpleNamespace(id="List/shop", title="Shopping", deleted=False),
        ]

    def get(self, reminder_id):
        return self.items[reminder_id]

    def update(self, item):
        self.updated.append(item)

    def create(self, **kwargs):
        self.created.append(kwargs)
        return Item("Reminder/new", kwargs["title"], kwargs["list_id"], due_date=kwargs["due_date"])


def icloud_with(service):
    api = SimpleNamespace(reminders=service, data={"ok": True}, requires_2fa=False)
    api.is_trusted_session = True
    return ICloudWeb("sam@example.com", "unused", opener=lambda *_: api)


async def test_change_feed_is_read_whole_once_then_only_changes():
    service = FakeService([Item("Reminder/a", "Bread"), Item("Reminder/b", "Milk", "List/shop")])
    source = ICloudReminders(icloud_with(service), [], None)

    assert {r.title for r in await source.open_reminders()} == {"Bread", "Milk"}
    service.feed = [SimpleNamespace(type="deleted", reminder_id="Reminder/b", reminder=None)]
    assert [r.title for r in await source.open_reminders()] == ["Bread"]
    assert service.cursors == [None, "c1"]


async def test_only_the_lists_asked_for():
    service = FakeService([Item("Reminder/a", "Bread"), Item("Reminder/b", "Milk", "List/shop")])
    source = ICloudReminders(icloud_with(service), ["shopping"], None)

    assert [r.list_name for r in await source.open_reminders()] == ["Shopping"]
    with pytest.raises(ToolError, match="get_reminders"):
        await source.complete("Reminder/a")


async def test_new_reminder_goes_to_the_busiest_list_in_the_home_zone(ctx):
    service = FakeService([Item("Reminder/a", "Bread"), Item("Reminder/b", "Milk", "List/shop")])
    service.items["Reminder/c"] = Item("Reminder/c", "Eggs", "List/shop")
    service.feed.append(
        SimpleNamespace(
            type="updated", reminder_id="Reminder/c", reminder=service.items["Reminder/c"]
        )
    )
    create = tool(
        build_reminder_tools(ICloudReminders(icloud_with(service), [], None), "Europe/Rome"),
        "create_reminder",
    )

    result = await create.handler({"title": " Call  Jo ", "due": "2026-11-03 09:00"}, ctx)

    (call,) = service.created
    assert call["list_id"] == "List/shop" and call["title"] == "Call Jo"
    assert call["due_date"] == datetime(2026, 11, 3, 9, 0, tzinfo=ROME)
    assert call["time_zone"] == "Europe/Rome" and call["all_day"] is False
    assert result["created"]["list"] == "Shopping"


async def test_all_day_reminder_floats_at_midnight_utc():
    service = FakeService([])
    source = ICloudReminders(icloud_with(service), [], "TO DO")

    await source.create("Holidays", None, date(2026, 12, 20), "")

    (call,) = service.created
    assert call["list_id"] == "List/todo" and call["all_day"] is True
    assert call["due_date"] == datetime(2026, 12, 20, tzinfo=UTC) and call["time_zone"] is None


async def test_unknown_list_names_the_lists():
    source = ICloudReminders(icloud_with(FakeService([])), [], None)
    with pytest.raises(ToolError, match="Shopping, TO DO"):
        await source.create("x", "Work", None, "")


async def test_done_reminder_is_checked():
    service = FakeService([Item("Reminder/a", "Bread")])
    source = ICloudReminders(icloud_with(service), [], None)

    assert await source.complete("Reminder/a") is None
    assert service.updated[0].completed is True


PLAIN_WEEKLY = {"Frequency": WEEKLY, "Interval": 1, "Deleted": 0, "Reminder": "x"}
MONTH_END = {**PLAIN_WEEKLY, "Frequency": MONTHLY, "DaysOfTheMonth": b"[28,29,30,31]"}


async def test_repeating_reminder_moves_to_its_next_day_in_its_zone():
    nine = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)  # Monday 9:00 in Rome, summer time
    item = Item("Reminder/a", "Plants", due_date=nine, time_zone="Europe/Rome")
    item.recurrence_rule_ids = ["Rule/1"]
    service = FakeService([item], rules={"RecurrenceRule/Rule/1": PLAIN_WEEKLY})
    source = ICloudReminders(icloud_with(service), [], None)

    following = await source.complete("Reminder/a")

    assert service.updated[0].completed is False
    assert following.due.astimezone(ROME).hour == 9  # after the change to winter time
    assert following.due.date() > datetime.now(ROME).date()
    assert following.due.weekday() == 0


async def test_repeating_reminder_on_a_rule_it_cant_follow_is_left_to_the_phone():
    item = Item("Reminder/a", "Rent", due_date=datetime(2026, 10, 31, 9, 0, tzinfo=UTC))
    item.recurrence_rule_ids = ["Rule/1"]
    service = FakeService([item], rules={"RecurrenceRule/Rule/1": MONTH_END})
    source = ICloudReminders(icloud_with(service), [], None)

    with pytest.raises(ToolError, match="iPhone"):
        await source.complete("Reminder/a")
    assert service.updated == []


async def test_an_expired_login_asks_to_log_in_again():
    from pyicloud.exceptions import PyiCloudFailedLoginException

    def expired(*_):
        raise PyiCloudFailedLoginException("No password set")

    source = ICloudReminders(ICloudWeb("sam@example.com", "unused", opener=expired), [], None)
    with pytest.raises(ToolError) as caught:
        await source.open_reminders()
    assert str(caught.value) == RENEW
    # Run anywhere but in the container, the login leaves the agent's session untouched.
    assert "docker compose exec agent python -m intelligent_home_agent.icloud_web" in RENEW
    assert "Logged in" in RENEW

    untrusted = SimpleNamespace(data={"ok": 1}, requires_2fa=True, is_trusted_session=False)
    source = ICloudReminders(ICloudWeb("me", "unused", opener=lambda *_: untrusted), [], None)
    with pytest.raises(ToolError, match="log in again"):
        await source.open_reminders()


class Recording(FakeReminders):
    async def create(self, title, list_name, due, notes):
        self.created = due
        return Reminder("new", title, due, list_name or "TO DO")

    async def update(self, reminder_id, title, due, notes):
        self.updated = due
        return Reminder(reminder_id, title or "Test", due, "TO DO")


async def test_in_minutes_counts_from_when_the_reminder_is_written(ctx, monkeypatch):
    # "in 2 minutes" taken from the prompt's time, after a slow request, was
    # already late when written.
    from intelligent_home_agent.tools import reminders as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 12, 11, 40, 15, tzinfo=ROME)

    monkeypatch.setattr(module, "datetime", Clock)
    source = Recording([])
    tools = build_reminder_tools(source, "Europe/Rome")

    created = await tool(tools, "create_reminder").handler({"title": "Hello", "in_minutes": 2}, ctx)
    assert source.created == datetime(2026, 11, 12, 11, 43, tzinfo=ROME)
    assert created["created"]["overdue"] is False
    await tool(tools, "update_reminder").handler({"id": "r1", "in_minutes": "5"}, ctx)
    assert source.updated == datetime(2026, 11, 12, 11, 46, tzinfo=ROME)
    for wrong in (0, "two", 99_999):
        with pytest.raises(ToolError, match="in_minutes"):
            await tool(tools, "create_reminder").handler(
                {"title": "Hello", "in_minutes": wrong}, ctx
            )

    line = await tool(tools, "create_reminder").summary({"title": "Hello", "in_minutes": 2})
    assert line == 'Create the reminder "Hello", 2 min after your go'


async def test_a_reminder_after_reading_them_waits_for_the_users_go_then_is_written(ctx):
    from intelligent_home_agent.main import with_waiting
    from intelligent_home_agent.tools import ToolContext, ToolRegistry
    from intelligent_home_agent.tools.proposals import build_proposal_tools

    source = Recording(REMINDERS)
    registry = ToolRegistry(build_reminder_tools(source, "Europe/Rome"))
    registry.extend(build_proposal_tools(registry))
    conv = ctx.conversation
    await registry.run("get_reminders", {}, ctx)
    ctx.tainted = True  # set by the agent's loop once the batch that read has run
    args = {"title": "Call Jo", "due": "2026-11-03 09:00"}

    parked = await registry.run("create_reminder", args, ctx)

    assert parked["status"] == "waiting_for_the_user" and not hasattr(source, "created")
    assert 'Create the reminder "Call Jo"' in with_waiting("Here.", ctx.actions, conv)
    conv.turn += 1
    await registry.run("run_proposals", {}, ToolContext(conversation=conv))
    assert source.created == datetime(2026, 11, 3, 9, 0, tzinfo=ROME)
