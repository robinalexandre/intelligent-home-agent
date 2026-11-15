from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tasks import TaskReports, TaskRunner, built_in_tasks, says_nothing
from intelligent_home_agent.tools import LocalFolder, ToolError

PARIS = ZoneInfo("Europe/Paris")


class FakeRepo:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def named_files(self) -> dict[str, str]:
        return self.files


def task(title: str, on: str, body: str = "Prepare the file.", until: str = "") -> str:
    return f"---\ntitle: {title}\non: {on}\nuntil: {until}\n---\n\n{body}\n"


async def test_a_due_task_starts_once_even_across_restarts(tmp_path):
    repo = FakeRepo(
        {
            "a-plants.md": task("Plants", "2026-01-01", "Water the plants on the balcony."),
            "b-later.md": task("Later", "2999-01-01"),
            "c-over.md": task("Over", "2026-01-01", until="2026-01-02"),
            "d-empty.md": task("Empty", "2026-01-01", body=""),
        }
    )
    started: list[tuple[str, str]] = []

    async def start(title: str, body: str, _repeating: bool) -> None:
        started.append((title, body))

    done = tmp_path / "done.json"
    runner = TaskRunner(repo, start, done, "Europe/Paris")  # type: ignore[arg-type]
    await runner.check_once()
    await runner.check_once()
    await TaskRunner(repo, start, done, "Europe/Paris").check_once()  # type: ignore[arg-type]

    assert started == [("Plants", "Water the plants on the balcony.")]


async def test_a_started_task_moves_to_past_and_a_failed_move_still_runs_once(tmp_path):
    repo = FakeRepo({"a.md": task("Call the plumber", "2026-01-01")})
    closed: list[str] = []
    started: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        started.append(title)

    async def close(name: str, _at: str) -> str:
        closed.append(name)
        raise ToolError("GitHub unavailable")

    runner = TaskRunner(repo, start, tmp_path / "d.json", "Europe/Paris", closer=close)  # type: ignore[arg-type]
    await runner.check_once()
    await runner.check_once()

    assert closed == ["a.md"] and started == ["Call the plumber"]


async def test_a_failing_task_is_not_run_again(tmp_path):
    repo = FakeRepo({"a.md": task("Plants", "2026-01-01")})
    calls = 0

    async def start(_title: str, _body: str, _repeating: bool) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    runner = TaskRunner(repo, start, tmp_path / "done.json", "Europe/Paris")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await runner.check_once()
    await runner.check_once()

    assert calls == 1


async def test_a_repeating_task_runs_once_per_day_at_its_time_and_stays(tmp_path):
    every = "---\ntitle: Backup check\non: 2026-10-22 12:00\nevery: 3 days\n---\n\nLook."
    repo = FakeRepo({"q.md": every})
    runs: list[bool] = []
    closed: list[str] = []

    async def start(_title: str, _body: str, repeating: bool) -> None:
        runs.append(repeating)

    async def close(name: str, _at: str) -> str:
        closed.append(name)
        return name

    runner = TaskRunner(repo, start, tmp_path / "d.json", "Europe/Paris", closer=close)  # type: ignore[arg-type]
    await runner.check_once(datetime(2026, 10, 22, 11, 59, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 12, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 18, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 12, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 25, 12, 30, tzinfo=PARIS))

    assert runs == [True, True] and closed == []


async def test_a_task_can_run_several_times_a_day(tmp_path):
    text = (
        "---\ntitle: Parcels\non: 2026-10-22\nevery: day\ntimes: 08:00, 12:00, 16:00\n---\n\nSee."
    )
    runs: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        runs.append(title)

    runner = TaskRunner(FakeRepo({"c.md": text}), start, tmp_path / "d.json", "Europe/Paris")  # type: ignore[arg-type]
    for hour, minute in ((7, 0), (8, 0), (9, 0), (12, 5), (17, 0), (17, 1)):
        await runner.check_once(datetime(2026, 10, 22, hour, minute, tzinfo=PARIS))

    assert len(runs) == 3


async def test_a_repeating_task_is_closed_for_the_day_after_its_last_run(tmp_path):
    files = {
        "q.md": "---\ntitle: Backups\non: 2026-10-22 12:00\nevery: day\n---\n\nLook.",
        "c.md": "---\ntitle: Parcels\non: 2026-10-22\nevery: day\ntimes: 08:00, 16:00\n---\n\nSee.",
    }
    runs: list[str] = []
    closed: list[tuple[str, str]] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        runs.append(title)
        if runs.count("Backups") == 1 and title == "Backups":
            raise RuntimeError("boom")

    async def close_day(name: str, day: str) -> None:
        closed.append((name, day))
        if name == "c.md":
            raise ToolError("GitHub unavailable")

    runner = TaskRunner(
        FakeRepo(files),  # type: ignore[arg-type]
        start,
        tmp_path / "d.json",
        "Europe/Paris",
        day_closer=close_day,
    )
    with pytest.raises(RuntimeError):
        await runner.check_once(datetime(2026, 10, 22, 12, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 16, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 8, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 12, 0, tzinfo=PARIS))

    assert runs == ["Parcels", "Backups", "Parcels", "Parcels", "Backups"]
    assert closed == [("c.md", "2026-10-22"), ("q.md", "2026-10-23")]


async def test_a_task_the_user_closed_for_today_skips_the_day(tmp_path):
    text = "---\ntitle: Backups\non: 2026-10-22 12:00\nevery: day\ndone: 2026-10-22\n---\n\nLook."
    runs: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        runs.append(title)

    runner = TaskRunner(FakeRepo({"q.md": text}), start, tmp_path / "d.json", "Europe/Paris")  # type: ignore[arg-type]
    await runner.check_once(datetime(2026, 10, 22, 12, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 12, 0, tzinfo=PARIS))

    assert runs == ["Backups"]


async def test_tasks_due_with_a_digest_run_inside_it(tmp_path):
    every = "---\ntitle: {}\non: 2026-10-22 09:00\nevery: day\n{}\n---\n\nV."
    files = {
        "a-parcels.md": every.format("Parcels", "times: 09:00, 11:00"),
        "b-bins.md": every.format("Bins", ""),
        "c-brief.md": every.format("Briefing", "digest: yes"),
    }
    alone: list[str] = []
    digests: list[tuple[str, list[str]]] = []
    closed: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        alone.append(title)

    async def digest(title: str, _body: str, gathered: list[tuple[str, str]]) -> None:
        digests.append((title, [t for t, _ in gathered]))

    async def close_day(name: str, _day: str) -> None:
        closed.append(name)

    runner = TaskRunner(
        FakeRepo(files),  # type: ignore[arg-type]
        start,
        tmp_path / "d.json",
        "Europe/Paris",
        day_closer=close_day,
        digest=digest,
    )
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 9, 1, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 11, 0, tzinfo=PARIS))

    assert digests == [("Briefing", ["Parcels", "Bins"])]
    assert alone == ["Parcels"]
    assert closed == ["b-bins.md", "c-brief.md", "a-parcels.md"]


async def test_a_task_with_digest_no_keeps_its_own_message(tmp_path):
    every = "---\ntitle: {}\non: 2026-10-22 09:00\nevery: day\n{}\n---\n\nV."
    files = {
        "a-garden.md": every.format("Garden", "digest: no"),
        "b-bins.md": every.format("Bins", ""),
        "c-brief.md": every.format("Briefing", "digest: yes"),
    }
    alone: list[tuple[str, bool]] = []
    digests: list[tuple[str, list[str]]] = []

    async def start(title: str, _body: str, repeating: bool) -> None:
        alone.append((title, repeating))

    async def digest(title: str, _body: str, gathered: list[tuple[str, str]]) -> None:
        digests.append((title, [t for t, _ in gathered]))

    runner = TaskRunner(
        FakeRepo(files),  # type: ignore[arg-type]
        start,
        tmp_path / "d.json",
        "Europe/Paris",
        digest=digest,
    )
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))

    assert alone == [("Garden", True)]
    assert digests == [("Briefing", ["Bins"])]


async def test_a_fresh_task_runs_clean_with_its_previous_run_and_never_in_a_digest(tmp_path):
    every = "---\ntitle: {}\non: 2026-10-22 09:00\nevery: {}\n{}\n---\n\nV."
    files = {
        "a-watch.md": every.format("Tech watch", "2 days", "fresh: yes"),
        "b-bins.md": every.format("Bins", "day", ""),
        "c-brief.md": every.format("Briefing", "day", "digest: yes"),
    }
    alone: list[tuple[str, str, bool]] = []
    digests: list[tuple[str, list[str]]] = []
    closed: list[str] = []
    fresh_runs: list[bool] = []

    async def start(title: str, body: str, repeating: bool, fresh: bool = False) -> None:
        alone.append((title, body, repeating))
        fresh_runs.append(fresh)

    async def digest(title: str, _body: str, gathered: list[tuple[str, str]]) -> None:
        digests.append((title, [t for t, _ in gathered]))

    async def close_day(name: str, _day: str) -> None:
        closed.append(name)

    runner = TaskRunner(
        FakeRepo(files),  # type: ignore[arg-type]
        start,
        tmp_path / "d.json",
        "Europe/Paris",
        day_closer=close_day,
        digest=digest,
    )
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 9, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 24, 9, 0, tzinfo=PARIS))

    assert digests == [("Briefing", ["Bins"])] * 3
    assert [(t, r) for t, _, r in alone] == [("Tech watch", False)] * 2
    assert fresh_runs == [True, True]  # silent when nothing came out
    assert "first run" in alone[0][1]
    assert "when nothing did, answer NOTHING_NEW" in alone[1][1]
    assert alone[1][1].startswith("V.") and "previous run was on 2026-10-22 09:00" in alone[1][1]
    assert closed.count("a-watch.md") == 2


async def test_a_fresh_task_alone_does_not_wake_the_digest(tmp_path):
    files = {
        "a.md": "---\ntitle: Watch\non: 2026-10-22 09:00\nevery: day\nfresh: yes\n---\n\nV.",
        "b.md": "---\ntitle: Briefing\non: 2026-10-22 08:00\nevery: day\ndigest: yes\n---\n\nB.",
    }
    alone: list[str] = []
    digests: list[str] = []

    async def start(title: str, _body: str, _repeating: bool, fresh: bool = False) -> None:
        alone.append(title)

    async def digest(title: str, _body: str, _gathered: list[tuple[str, str]]) -> None:
        digests.append(title)

    runner = TaskRunner(FakeRepo(files), start, tmp_path / "d.json", "Europe/Paris", digest=digest)  # type: ignore[arg-type]
    await runner.check_once(datetime(2026, 10, 22, 8, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))

    assert alone == ["Briefing", "Watch"] and digests == []


def test_the_last_report_of_a_task_is_kept(tmp_path):
    reports = TaskReports(tmp_path / "r.json")
    assert reports.get("Backups") is None
    reports.put("Backups", "2 backups missing", datetime(2026, 10, 22, 12, 0))
    assert TaskReports(tmp_path / "r.json").get("Backups") == {
        "at": "2026-10-22 12:00",
        "text": "2 backups missing",
    }


def test_a_task_keeps_the_reports_before_its_last_one(tmp_path):
    reports = TaskReports(tmp_path / "r.json")
    for day, text in enumerate(["Critical flaw in left-pad", "Web search skipped", "x"]):
        reports.put("Watch", text, datetime(2026, 11, 3 + day, 8, 0))
    reports.put("Watch", "Nothing more" + "y" * 4000, datetime(2026, 11, 8, 8, 0))

    last = TaskReports(tmp_path / "r.json").get("Watch")
    assert last["at"] == "2026-11-08 08:00" and len(last["text"]) == 3000
    assert last["earlier"] == [
        {"at": "2026-11-05 08:00", "text": "x"},
        {"at": "2026-11-04 08:00", "text": "Web search skipped"},
    ]
    reports.put("Watch", "Again", datetime(2026, 11, 9, 8, 0))
    assert [len(r["text"]) for r in reports.get("Watch")["earlier"]] == [1500, 1]


async def test_a_repeating_task_refused_a_search_runs_clean_from_then_on(tmp_path):
    every = "---\ntitle: {}\non: 2026-10-22 09:00\nevery: day\n{}\n---\n\nV."
    files = {
        "a-watch.md": every.format("Security watch", ""),
        "b-parcels.md": every.format("Parcels", "fresh: no"),
    }
    runs: list[tuple[str, bool, bool]] = []

    async def start(title: str, body: str, repeating: bool, fresh: bool = False) -> bool:
        runs.append((title, repeating, fresh))
        return repeating

    runner = TaskRunner(FakeRepo(files), start, tmp_path / "d.json", "Europe/Paris")  # type: ignore[arg-type]
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 9, 0, tzinfo=PARIS))

    assert runs == [
        ("Security watch", True, False),
        ("Parcels", True, False),
        # The search waits no more for a line the user would have to add to the file.
        ("Security watch", False, True),
        ("Parcels", True, False),
    ]


async def test_a_gathered_task_refused_a_search_leaves_the_digest_but_the_digest_stays(tmp_path):
    every = "---\ntitle: {}\non: 2026-10-22 09:00\nevery: day\n{}\n---\n\nV."
    files = {
        "a-watch.md": every.format("Watch", ""),
        "b-bins.md": every.format("Bins", ""),
        "c-brief.md": every.format("Briefing", "digest: yes"),
    }
    digests: list[list[str]] = []
    alone: list[tuple[str, bool]] = []

    async def start(title: str, _body: str, _repeating: bool, fresh: bool = False) -> None:
        alone.append((title, fresh))

    async def digest(title: str, _body: str, gathered: list[tuple[str, str]]) -> set[str]:
        digests.append([t for t, _ in gathered])
        return {"Watch", "Briefing"}

    runner = TaskRunner(FakeRepo(files), start, tmp_path / "d.json", "Europe/Paris", digest=digest)  # type: ignore[arg-type]
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 9, 0, tzinfo=PARIS))

    assert digests == [["Watch", "Bins"], ["Bins"]]
    assert alone == [("Watch", True)]


async def test_a_task_with_a_date_written_wrong_never_stops_the_others(tmp_path):
    files = {
        "a.md": "---\ntitle: Wrong\non: next week\nevery: day\n---\n\nV.",
        "b.md": "---\ntitle: Right\non: 2026-10-22 09:00\nevery: day\n---\n\nV.",
    }
    runs: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        runs.append(title)

    runner = TaskRunner(FakeRepo(files), start, tmp_path / "d.json", "Europe/Paris")  # type: ignore[arg-type]
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))

    assert runs == ["Right"]


async def test_the_briefing_from_the_settings_runs_as_a_digest_and_a_file_replaces_it(tmp_path):
    built_in = built_in_tasks(strings("fr"), "7:30", "19:00")
    assert list(built_in) == ["morning-briefing.md", "evening-recap.md"]
    assert built_in_tasks(strings("en"), None, "") == {}
    files = {"bins.md": "---\ntitle: Server\non: 2026-10-22 07:30\nevery: day\n---\n\nCheck it."}
    digests: list[tuple[str, str, list[str]]] = []
    alone: list[str] = []
    closed: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        alone.append(title)

    async def digest(title: str, body: str, gathered: list[tuple[str, str]]) -> None:
        digests.append((title, body, [t for t, _ in gathered]))

    async def close_day(name: str, _day: str) -> None:
        closed.append(name)

    repo = FakeRepo(files)
    runner = TaskRunner(
        repo,  # type: ignore[arg-type]
        start,
        tmp_path / "d.json",
        "Europe/Paris",
        day_closer=close_day,
        digest=digest,
        built_in=built_in,
    )
    await runner.check_once(datetime(2026, 10, 22, 7, 29, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 7, 30, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 19, 0, tzinfo=PARIS))

    assert digests == [("Briefing du matin", "Fais-moi le briefing du matin", ["Server"])]
    assert alone == ["Récap du soir"]
    # Only what is in the folder is closed there: a task from the settings has no file.
    assert closed == ["bins.md"]

    repo.files["morning-briefing.md"] = (
        "---\ntitle: My morning\non: 2026-10-01 06:00\nevery: weekday\n---\n\nOnly the weather."
    )
    await runner.check_once(datetime(2026, 10, 23, 6, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 23, 7, 30, tzinfo=PARIS))
    assert alone == ["Récap du soir", "My morning", "Server"]


async def test_a_local_task_runs_once_and_moves_to_past(tmp_path):
    folder = LocalFolder(tmp_path / "tasks")
    name = await folder.create("Call the plumber", task("Call the plumber", "2026-01-01"), "x")
    await folder.put("daily", "---\ntitle: Daily\non: 2026-10-22 09:00\nevery: day\n---\n\nV.")
    runs: list[str] = []

    async def start(title: str, _body: str, _repeating: bool) -> None:
        runs.append(title)

    runner = TaskRunner(
        folder, start, tmp_path / "d.json", "Europe/Paris", 60, folder.close, folder.close_for_day
    )
    await runner.check_once(datetime(2026, 10, 22, 9, 0, tzinfo=PARIS))
    await runner.check_once(datetime(2026, 10, 22, 9, 1, tzinfo=PARIS))

    assert runs == ["Call the plumber", "Daily"]
    assert (tmp_path / "tasks" / "past" / name).is_file()
    assert "\ndone: 2026-10-22\n" in await folder.text("daily")
    assert list(await folder.named_files()) == ["daily.md"]


def test_a_silent_reply_may_come_with_a_sentence_before_it():
    assert says_nothing("NOTHING_NEW") and says_nothing("Nothing on parcels.\nNOTHING_NEW\n")
    assert not says_nothing("NOTHING_NEW is what I would say, but the server is down.")
