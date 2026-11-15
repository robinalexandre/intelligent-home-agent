from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.jobs import (
    DENIED,
    DONE,
    FAILED,
    GONE,
    IDLE,
    RUNNING,
    STOPPED,
    Event,
    Job,
    JobRunner,
    JobStore,
    SessionGone,
)
from intelligent_home_agent.jobs_managed import ManagedAgentsBackend
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry, build_job_tools

ROOT = Path(__file__).resolve().parents[1]


class FakeBackend:
    """Sessions whose events the test feeds: the first ones at once, the rest when pushed.
    A new follow replays everything seen so far, like a real backend's history."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str]] = []
        self.interrupted: list[str] = []
        self.history: dict[str, list[Event]] = {}
        self.pushed: dict[str, asyncio.Queue] = {}

    def feed(self, session: str, *events: Event) -> None:
        self.history.setdefault(session, []).extend(events)
        for event in events:
            self.pushed.setdefault(session, asyncio.Queue()).put_nowait(event)

    async def start(self, job_id, title, brief, rubric, repo, branch, model) -> str:
        self.started.append(
            {
                "id": job_id,
                "title": title,
                "brief": brief,
                "rubric": rubric,
                "repo": repo,
                "model": model,
            }
        )
        return f"s-{job_id}"

    async def send(self, session: str, text: str) -> None:
        self.sent.append((session, text))

    async def interrupt(self, session: str) -> None:
        self.interrupted.append(session)

    async def follow(self, session: str):
        queue = self.pushed.setdefault(session, asyncio.Queue())
        for event in list(self.history.get(session, [])):
            yield event
        while True:
            yield await queue.get()

    def console_url(self, session: str) -> str:
        return f"http://coder/jobs/{session}"


class Reports:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []
        self.arrived = asyncio.Event()

    async def __call__(self, job: Job, text: str) -> None:
        self.items.append((job.id, job.status, text))
        self.arrived.set()

    async def wait(self, count: int = 1) -> None:
        while len(self.items) < count:
            self.arrived.clear()
            await asyncio.wait_for(self.arrived.wait(), 2)


def runner(tmp_path: Path, backend: Any, reports: Reports) -> JobRunner:
    return JobRunner(
        backend,
        JobStore(tmp_path / "jobs.json"),
        report=reports,
        repo_path="/work/{job}/{name}",
        git_name="Sam",
        git_email="sam@example.com",
        user_name="Sam",
        models={"complex": "claude-test-big"},
        reconnect_seconds=0.01,
    )


def stopped(at: str = "2026-10-23T10:00:00Z", status: str = IDLE) -> Event:
    return Event("stopped", status=status, at=at)


async def test_a_job_briefs_the_coder_and_reports_when_it_stops(tmp_path):
    backend, reports = FakeBackend(), Reports()
    jobs = runner(tmp_path, backend, reports)

    job = await jobs.start("Price cache", "Add a cache.", "me/shop", "develop")
    backend.feed(
        job.session_id,
        Event("said", "Reading the code."),
        Event("said", "Done: https://github.com/me/shop/pull/12 (tests pass)."),
        stopped(),
    )
    await reports.wait()
    await jobs.aclose()

    started = backend.started[0]
    assert started["id"] == job.id and started["repo"] == "me/shop"
    assert "Add a cache." in started["brief"]
    assert f"cloned at /work/{job.id}/shop (branch develop)" in started["brief"]
    assert "Sam <sam@example.com>" in started["brief"]
    assert "me/shop" in started["rubric"]
    assert started["model"] == "claude-opus-5-5" == job.model
    said = "Reading the code.\nDone: https://github.com/me/shop/pull/12 (tests pass)."
    assert reports.items == [(job.id, IDLE, said)]
    kept = JobStore(tmp_path / "jobs.json").get(job.id)
    assert kept.status == IDLE and kept.pr_url == "https://github.com/me/shop/pull/12"
    assert kept.reported_at == "2026-10-23T10:00:00Z" and kept.session_id == f"s-{job.id}"
    assert kept.model == "claude-opus-5-5" and kept.summary()["model"] == "claude-opus-5-5"
    assert jobs.console_url(kept) == f"http://coder/jobs/s-{job.id}"


async def test_a_job_the_user_waits_for_is_tracked_with_what_its_coder_last_said(tmp_path):
    from intelligent_home_agent.progress import Tracker

    async def send(text):
        pass

    backend, reports = FakeBackend(), Reports()
    news = Tracker(send, tmp_path / "durations.json", "en")
    jobs = runner(tmp_path, backend, reports)
    jobs._tracker = news

    job = await jobs.start("Cache", "Add a cache.", "me/shop", announce=True)
    quiet = await jobs.start("Review", "Read the day again.", "me/agent", quiet=True, announce=True)
    assert jobs.estimate_minutes(job) == 25 and jobs.estimate_minutes(quiet) is None
    backend.feed(job.session_id, Event("said", "Running the tests.\nTests pass."))
    for _ in range(100):
        if "Tests pass" in news.line():
            break
        await asyncio.sleep(0.01)
    assert news.line().startswith(f"⏳ Job {job.id}: ~0%") and '"Tests pass."' in news.line()

    backend.feed(job.session_id, stopped())
    await reports.wait()
    assert news.line() == "⏳ " and "job_normal" in news._durations()
    # The user's follow-up is a new wait of its own.
    await jobs.ask(job.id, "Add a test.", announce=True)
    assert news.line().startswith(f"⏳ Job {job.id}") and jobs.estimate_minutes(job) == 10
    await jobs.aclose()


async def test_a_quiet_job_ends_at_its_first_stop(tmp_path):
    backend, reports = FakeBackend(), Reports()
    jobs = runner(tmp_path, backend, reports)

    job = await jobs.start("Review", "Read the day again.", "me/agent", quiet=True)
    backend.feed(job.session_id, Event("said", "Nothing to fix."), stopped())
    await reports.wait()
    await jobs.aclose()

    assert reports.items == [(job.id, DONE, "Nothing to fix.")]
    kept = JobStore(tmp_path / "jobs.json").get(job.id)
    assert kept.quiet and kept.status == DONE


async def test_a_job_without_a_repo_gets_no_rubric(tmp_path):
    backend = FakeBackend()
    jobs = runner(tmp_path, backend, Reports())
    await jobs.start("Audit", "Explain X to me.")
    await jobs.aclose()
    assert backend.started[0]["rubric"] == "" and "cloned" not in backend.started[0]["brief"]


async def test_the_complexity_picks_the_model(tmp_path):
    backend = FakeBackend()
    jobs = runner(tmp_path, backend, Reports())
    simple = await jobs.start("Typo", "Fix the typo.", "me/shop", complexity="simple")
    complex_ = await jobs.start("Rework", "Redo the cache.", "me/shop", complexity="complex")
    await jobs.aclose()
    assert simple.model == "claude-sonnet-5-5" and complex_.model == "claude-test-big"
    assert [s["model"] for s in backend.started] == ["claude-sonnet-5-5", "claude-test-big"]
    with pytest.raises(ToolError, match="complexity must be one of"):
        await jobs.start("x", "y", "me/shop", complexity="huge")
    assert len(backend.started) == 2


async def test_a_session_the_runner_lost_closes_the_job(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.put(Job("old1", "Fix", "Fix it.", "sesn_other", "me/shop", status=RUNNING))

    class Forgetful(FakeBackend):
        async def follow(self, session: str):
            raise SessionGone("No such job")
            yield  # pragma: no cover

    reports = Reports()
    jobs = runner(tmp_path, Forgetful(), reports)
    await jobs.resume()
    await reports.wait()
    await jobs.aclose()
    assert reports.items == [("old1", STOPPED, GONE)]
    assert store.get("old1").status == STOPPED and store.get("old1").report == GONE


async def test_the_user_talks_to_a_job_and_stops_it(tmp_path):
    backend, reports = FakeBackend(), Reports()
    jobs = runner(tmp_path, backend, reports)
    job = await jobs.start("Fix", "Fix it.", "me/shop")
    backend.feed(job.session_id, stopped())
    await reports.wait()
    assert reports.items == [(job.id, IDLE, "")]

    asked = await jobs.ask(job.id, "Use the cache too.")
    assert asked.status == RUNNING
    assert backend.sent == [(job.session_id, "Use the cache too.")]

    stopping = await jobs.stop(f"#{job.id}")
    assert stopping.status == RUNNING and stopping.closing
    assert backend.interrupted == [job.session_id]

    backend.feed(job.session_id, Event("said", "Stopped there."), stopped("2026-10-23T10:05:00Z"))
    await reports.wait(2)
    await jobs.aclose()
    assert reports.items[1] == (job.id, STOPPED, "Stopped there.")
    assert jobs.get(job.id).status == STOPPED
    with pytest.raises(ToolError, match="is stopped"):
        await jobs.ask(job.id, "Again?")
    assert (await jobs.stop(job.id)).status == STOPPED


async def test_an_idle_job_stops_at_once(tmp_path):
    backend, reports = FakeBackend(), Reports()
    jobs = runner(tmp_path, backend, reports)
    job = await jobs.start("Fix", "Fix it.", "me/shop")
    backend.feed(job.session_id, stopped())
    await reports.wait()

    assert (await jobs.stop(job.id)).status == STOPPED
    await jobs.aclose()
    assert backend.interrupted == []


async def test_after_a_restart_only_stops_not_yet_reported_are_reported(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    job = Job("ab12", "Fix", "Fix it.", "s-ab12", "me/shop", status=RUNNING)
    job.reported_at = "2026-10-23T10:00:00Z"
    store.put(job)
    backend, reports = FakeBackend(), Reports()
    backend.history["s-ab12"] = [
        Event("said", "Old report."),
        stopped("2026-10-23T10:00:00Z"),
        Event("said", "New: https://github.com/me/shop/pull/3"),
        stopped("2026-10-23T10:30:00Z"),
        Event("ended", at="2026-10-23T11:00:00Z"),
    ]
    jobs = runner(tmp_path, backend, reports)

    await jobs.resume()
    await reports.wait(2)
    await jobs.aclose()

    assert [(s, t) for _, s, t in reports.items] == [
        (IDLE, "New: https://github.com/me/shop/pull/3"),
        (DONE, ""),
    ]
    kept = store.get("ab12")
    assert kept.status == DONE and kept.pr_url == "https://github.com/me/shop/pull/3"
    assert kept.summary()["pull_request"] == "https://github.com/me/shop/pull/3"


async def test_a_lost_connection_is_retried_and_a_closed_job_is_not_followed(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.put(Job("done1", "Fix", "Fix it.", "s-done1", status=DONE))
    store.put(Job("live1", "Fix", "Fix it.", "s-live1", status=RUNNING))

    class Flaky(FakeBackend):
        calls = 0

        async def follow(self, session: str):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise ConnectionError("gone")
            async for event in super().follow(session):
                yield event

    backend, reports = Flaky(), Reports()
    backend.history["s-live1"] = [Event("said", "Ready."), stopped(status=FAILED)]
    jobs = runner(tmp_path, backend, reports)
    await jobs.resume()
    await reports.wait()
    await jobs.aclose()
    assert Flaky.calls == 2 and reports.items == [("live1", FAILED, "Ready.")]
    assert store.get("done1").status == DONE and "s-done1" not in backend.pushed


class FakeStream:
    def __init__(self, events: list[Any], pushed: asyncio.Queue) -> None:
        self._events = events
        self._pushed = pushed

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def __aiter__(self):
        for event in self._events:
            yield event
        while True:
            yield await self._pushed.get()


class FakeSessions:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.history: list[Any] = []
        self.live: list[Any] = []
        self.pushed: asyncio.Queue = asyncio.Queue()
        self.events = self

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(id="sesn_1", status="running")

    async def send(self, session_id: str, *, events: list[dict[str, Any]]) -> None:
        self.sent.extend((session_id, e) for e in events)

    async def stream(self, session_id: str) -> FakeStream:
        return FakeStream(self.live, self.pushed)

    async def list(self, session_id: str, *, order: str):
        assert order == "asc"
        for event in self.history:
            yield event


def managed(sessions: FakeSessions, **kwargs: Any) -> ManagedAgentsBackend:
    return ManagedAgentsBackend(
        sessions,
        agent_id="agent_1",
        environment_id="env_1",
        github_token="github_pat_x",
        vault_ids=["vlt_1"],
        **kwargs,
    )


async def test_managed_agents_start_a_session_on_the_repo_with_a_cap_and_an_outcome():
    sessions = FakeSessions()
    backend = managed(sessions, budget_usd=2.5)
    session = await backend.start(
        "ab12", "Cache", "The brief.", "The rubric.", "me/shop", "dev", "claude-opus-5-5"
    )
    assert session == "sesn_1"
    created = sessions.created[0]
    assert created["agent"] == "agent_1" and created["environment_id"] == "env_1"
    assert created["budget"]["max_list_cost"] == {"amount": "250", "currency": "USD"}
    assert created["vault_ids"] == ["vlt_1"] and created["metadata"] == {"job": "ab12"}
    assert created["resources"] == [
        {
            "type": "github_repository",
            "url": "https://github.com/me/shop",
            "authorization_token": "github_pat_x",
            "checkout": {"type": "branch", "name": "dev"},
        }
    ]
    brief, outcome = created["initial_events"]
    assert brief == {"type": "user.message", "content": [{"type": "text", "text": "The brief."}]}
    assert outcome["type"] == "user.define_outcome" and outcome["max_iterations"] == 5
    assert outcome["rubric"] == {"type": "text", "content": "The rubric."}
    assert backend.console_url("sesn_1").endswith("/workspaces/default/sessions/sesn_1")

    await backend.start("cd34", "Audit", "Explain.", "", "", "", "")
    assert "resources" not in sessions.created[1]
    assert [e["type"] for e in sessions.created[1]["initial_events"]] == ["user.message"]


def text(event_id: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="agent.message", id=event_id, content=[SimpleNamespace(type="text", text=body)]
    )


def idle(event_id: str, reason: str = "end_turn", at: Any = "2026-10-23T10:00:00Z"):
    return SimpleNamespace(
        type="session.status_idle",
        id=event_id,
        stop_reason=SimpleNamespace(type=reason),
        processed_at=at,
    )


async def test_managed_agents_events_become_what_the_coder_said_and_when_it_stopped():
    sessions = FakeSessions()
    # The real SDK stamps events with aware datetimes, which the runner must get as strings.
    stamped = datetime(2026, 10, 23, 11, 0, 0, 571834, tzinfo=timezone(timedelta(hours=2)))
    sessions.history = [text("h1", "Starting."), idle("h2", at=stamped)]
    sessions.live = [
        text("h2", "duplicate"),
        SimpleNamespace(type="agent.tool_use", id="e1", name="bash", evaluated_permission="ask"),
        idle("e2", "requires_action"),
        SimpleNamespace(type="agent.tool_use", id="e3", name="edit", evaluated_permission="allow"),
        SimpleNamespace(
            type="span.outcome_evaluation_end",
            id="e4",
            result="needs_revision",
            explanation="No tests.",
        ),
        SimpleNamespace(type="session.error", id="e5", error=SimpleNamespace(message="boom")),
        text("e6", "Going on."),
        idle("e7", "budget_reached", at="2026-10-23T10:00:00Z"),
        idle("e8", "retries_exhausted", at="2026-10-23T10:01:00Z"),
        SimpleNamespace(
            type="session.status_terminated", id="e9", processed_at="2026-10-23T11:00:00Z"
        ),
    ]
    backend = managed(sessions)

    seen: list[Event] = []
    async for event in backend.follow("sesn_1"):
        seen.append(event)
        if event.kind == "ended":
            break

    assert [(e.kind, e.text, e.status, e.at) for e in seen] == [
        ("said", "Starting.", IDLE, ""),
        ("stopped", "", IDLE, "2026-10-23T09:00:00.571834Z"),
        ("said", "⛔ Call to bash refused: nobody watches the run.", IDLE, ""),
        ("said", "Evaluation: needs_revision. No tests.", IDLE, ""),
        ("said", "⚠️ boom", IDLE, ""),
        ("said", "Going on.", IDLE, ""),
        (
            "said",
            "💸 The session's budget is reached: the job is paused.",
            IDLE,
            "2026-10-23T10:00:00Z",
        ),
        ("stopped", "", IDLE, "2026-10-23T10:00:00Z"),
        ("stopped", "", FAILED, "2026-10-23T10:01:00Z"),
        ("ended", "", IDLE, "2026-10-23T11:00:00Z"),
    ]
    assert sessions.sent == [
        (
            "sesn_1",
            {
                "type": "user.tool_confirmation",
                "tool_use_id": "e1",
                "result": "deny",
                "deny_message": DENIED,
            },
        )
    ]
    await backend.send("sesn_1", "More")
    await backend.interrupt("sesn_1")
    assert sessions.sent[1:] == [
        ("sesn_1", {"type": "user.message", "content": [{"type": "text", "text": "More"}]}),
        ("sesn_1", {"type": "user.interrupt"}),
    ]


def tool_set(jobs: JobRunner) -> ToolRegistry:
    return ToolRegistry(build_job_tools(jobs))


async def test_jobs_start_only_on_the_users_own_clean_message(tmp_path, ctx):
    backend = FakeBackend()
    jobs = runner(tmp_path, backend, Reports())
    tools = tool_set(jobs)
    args = {"title": "Cache", "request": "Add a cache\n- prices\n- stock", "repo": "me/shop"}

    tainted = ToolContext(conversation=ctx.conversation, tainted=True)
    parked = await tools.run("start_job", args, tainted)
    assert parked["status"] == "waiting_for_the_user"
    assert ctx.conversation.proposals[0].summary == (
        'Start the job "Cache" on me/shop: Add a cache ⏎ - prices ⏎ - stock'
    )
    scheduled = ToolContext(conversation=Conversation(id="t", turn=1), scheduled=True)
    assert (await tools.run("start_job", args, scheduled))["status"] == "waiting_for_the_user"
    assert backend.started == []

    started = await tools.run("start_job", args, ctx)
    simple = await tools.run("start_job", {**args, "complexity": "Simple"}, ctx)
    await jobs.aclose()
    assert {started["job"], simple["job"]} == {j.id for j in jobs.list()}
    assert started["console"] == f"http://coder/jobs/s-{started['job']}"
    assert backend.started[0]["title"] == "Cache"
    assert started["model"] == "claude-opus-5-5" and simple["model"] == "claude-sonnet-5-5"

    listed = await tools.run("list_jobs", {}, ctx)
    assert listed["jobs"][0]["status"] == RUNNING and tools.taints("list_jobs")


async def test_job_arguments_are_checked(tmp_path, ctx):
    tools = tool_set(runner(tmp_path, FakeBackend(), Reports()))
    with pytest.raises(ToolError, match="owner/name"):
        await tools.run("start_job", {"title": "x", "request": "y", "repo": "shop"}, ctx)
    with pytest.raises(ToolError, match="branch"):
        await tools.run(
            "start_job", {"title": "x", "request": "y", "repo": "me/shop", "branch": "a b"}, ctx
        )
    with pytest.raises(ToolError, match="request is required"):
        await tools.run("start_job", {"title": "x", "request": " ", "repo": "me/shop"}, ctx)
    with pytest.raises(ToolError, match="complexity"):
        await tools.run(
            "start_job",
            {"title": "x", "request": "y", "repo": "me/shop", "complexity": "huge"},
            ctx,
        )
    with pytest.raises(ToolError, match="No job"):
        await tools.run("ask_job", {"job": "zz", "message": "hello"}, ctx)
    assert build_job_tools(None) == []


def load_setup_script():
    spec = importlib.util.spec_from_file_location("jobs_setup", ROOT / "scripts" / "jobs_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_agent_and_environment_files_are_valid_create_bodies():
    setup = load_setup_script()
    agent = setup.load_agent()
    assert agent["name"] == "intelligent-home-agent coder"
    assert agent["model"] == {"id": "claude-opus-5-5", "effort": "high"}
    toolset = agent["tools"][0]
    assert toolset["type"] == "agent_toolset_20260401"
    assert toolset["default_config"]["permission_policy"] == {"type": "auto"}
    assert {c["name"]: c["enabled"] for c in toolset["configs"]} == {
        "web_fetch": False,
        "web_search": False,
    }
    assert agent["system"].startswith("You are a software engineer")
    assert "GH_TOKEN" in agent["system"] and "---" not in agent["system"]
    environment = setup.load_environment()
    assert environment["config"]["type"] == "cloud"
    assert environment["config"]["networking"]["type"] == "limited"
    assert "api.github.com" in environment["config"]["networking"]["allowed_hosts"]
