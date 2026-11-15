from datetime import datetime, time
from zoneinfo import ZoneInfo

from intelligent_home_agent.jobs import DONE, Job, JobStore
from intelligent_home_agent.journal import Journal
from intelligent_home_agent.review import (
    NightlyReview,
    journal_text,
    lessons,
    review_note,
    reviewed_day,
)

ZONE = "Europe/Brussels"
LOCAL = ZoneInfo(ZONE)


class FakeJobs:
    def __init__(self, jobs=()) -> None:
        self.jobs = list(jobs)
        self.started: list[dict] = []

    def list(self):
        return self.jobs

    async def start(self, title, request, repo="", branch="", complexity="normal", *, quiet=False):
        self.started.append(
            {"title": title, "request": request, "repo": repo, "quiet": quiet, "model": complexity}
        )
        return Job("r1", title, request, "s-r1", repo, quiet=quiet)


def entry(at, request, **extra):
    return {
        "at": at,
        "display": "chat",
        "request": request,
        "tools": [],
        "progress": 0,
        "reply_chars": 3,
        "seconds": 1.0,
        "outcome": "ok",
        "reply": "Ok.",
        **extra,
    }


async def private() -> bool:
    return True


def review(tmp_path, jobs, at=time(5, 30), **kwargs):
    kwargs.setdefault("is_private", private)
    return NightlyReview(
        Journal(tmp_path / "journal", ZONE),
        jobs,
        "me/my-agent",
        at,
        ZONE,
        tmp_path / "review.json",
        assistant_name="Nova",
        **kwargs,
    )


async def test_the_night_reviews_the_day_before_once(tmp_path):
    journal = Journal(tmp_path / "journal", ZONE)
    journal.add(
        entry(
            "2026-10-24T09:12:00+02:00",
            "File the hosting invoice",
            tools=[{"name": "save_file_to_drive", "error": "Waits for the user's go."}],
            outcome="fallback",
        )
    )
    user_job = Job(
        "a1",
        "Cache",
        "Add a cache.",
        "s-a1",
        "me/shop",
        DONE,
        created_at="2026-10-24T08:00:00Z",
    )
    jobs = FakeJobs([user_job])
    nightly = review(tmp_path, jobs)

    assert await nightly.check_once(datetime(2026, 10, 25, 5, 29, tzinfo=LOCAL)) is None
    job = await nightly.check_once(datetime(2026, 10, 25, 5, 30, tzinfo=LOCAL))
    assert await nightly.check_once(datetime(2026, 10, 25, 9, 0, tzinfo=LOCAL)) is None

    assert job is not None and job.quiet
    [started] = jobs.started
    assert started["title"] == "Nightly review of 2026-10-24"
    assert started["repo"] == "me/my-agent" and started["quiet"] is True
    request = started["request"]
    assert request.startswith("Nightly review of Nova's requests of 2026-10-24.")
    assert reviewed_day(job) == "2026-10-24"
    assert "fix/nightly-review/2026-10-24, opened on me/my-agent itself" in request
    assert "even the one\n  me/my-agent was forked from" in request
    assert "## 1. 09:12 (chat) - fallback" in request
    assert "User: File the hosting invoice" in request
    assert "- save_file_to_drive -> error: Waits for the user's go." in request
    assert 'Job a1 "Cache" on me/shop: done' in request
    assert "Never loosen a guardrail" in request and "The journal is private." in request


async def test_a_day_without_requests_starts_no_job(tmp_path):
    jobs = FakeJobs()
    nightly = review(tmp_path, jobs)
    assert await nightly.check_once(datetime(2026, 10, 25, 6, 0, tzinfo=LOCAL)) is None
    assert jobs.started == []


async def test_the_journal_only_goes_to_a_repository_known_to_be_private(tmp_path, caplog):
    async def public() -> bool:
        return False

    async def unknown() -> None:
        return None

    for day, kwargs in enumerate(
        ({"is_private": public}, {"is_private": unknown}, {"is_private": None}), 25
    ):
        folder = tmp_path / str(day)
        Journal(folder / "journal", ZONE).add(entry(f"2026-10-{day - 1}T10:00:00+02:00", "Hello"))
        jobs = FakeJobs()
        nightly = review(folder, jobs, **kwargs)
        assert await nightly.check_once(datetime(2026, 10, day, 6, 0, tzinfo=LOCAL)) is None
        assert jobs.started == []
        # Marked reviewed all the same: not asked again every five minutes.
        assert await nightly.check_once(datetime(2026, 10, day, 6, 5, tzinfo=LOCAL)) is None
    assert "REVIEW_ALLOW_PUBLIC" in caplog.text and "is public" in caplog.text

    # The user said a public repository is fine: no check is made.
    Journal(tmp_path / "journal", ZONE).add(entry("2026-10-24T10:00:00+02:00", "Hello"))
    jobs = FakeJobs()
    nightly = review(tmp_path, jobs, is_private=public, allow_public=True)
    assert await nightly.check_once(datetime(2026, 10, 25, 6, 0, tzinfo=LOCAL)) is not None


async def test_earlier_nightly_reviews_are_not_reviewed_again(tmp_path):
    journal = Journal(tmp_path / "journal", ZONE)
    journal.add(entry("2026-10-24T22:00:00+02:00", "Hello"))
    old = Job("r0", "Review", "…", "s-r0", "me/my-agent", DONE, created_at="2026-10-24T02:00:00Z")
    old.quiet = True
    jobs = FakeJobs([old])
    await review(tmp_path, jobs).check_once(datetime(2026, 10, 25, 6, 0, tzinfo=LOCAL))
    assert "(none)" in jobs.started[0]["request"]


def test_a_long_day_keeps_the_requests_that_went_wrong():
    quiet = [entry(f"2026-10-24T10:{i:02d}:00+02:00", "x" * 400) for i in range(30)]
    wrong = entry("2026-10-24T11:00:00+02:00", "Send the quote", outcome="out_of_rounds")
    text = journal_text([*quiet, wrong], limit=3000)
    assert "## 31. 11:00 (chat) - out_of_rounds" in text
    assert len(text) < 3200 and "quieter requests left out" in text


def test_the_lessons_of_a_review_become_a_note_for_the_curator():
    job = Job("r1", "Review", "Nightly review of Nova's requests of 2026-10-24.\n…", "s", "me/a")
    report = (
        "https://github.com/me/a/pull/41 Shorter replies\n\n"
        "Lessons:\n- Quotes need the message id, not its body.\nnoise\n- A list beats yes/no."
    )
    assert reviewed_day(job) == "2026-10-24"
    assert reviewed_day(Job("a1", "Cache", "Add a cache.", "s", "me/shop")) == ""
    assert lessons("Nothing to fix.") == ""
    assert review_note("2026-10-24", report) == (
        "# Nightly review of 2026-10-24\n\n"
        "https://github.com/me/a/pull/41 Shorter replies\n\n"
        "Lessons the review drew from the assistant's requests of that day:\n\n"
        "- Quotes need the message id, not its body.\n- A list beats yes/no.\n"
    )


def test_the_job_store_reads_jobs_saved_before_quiet_existed(tmp_path):
    (tmp_path / "jobs.json").write_text(
        '[{"id": "a1", "title": "t", "request": "r", "session_id": "s", "later_field": 1}]'
    )
    assert JobStore(tmp_path / "jobs.json").get("a1").quiet is False
