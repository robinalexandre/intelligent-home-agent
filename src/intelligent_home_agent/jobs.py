"""Jobs the agent hands to a coding subagent that works on its own.

A job is a coding session the user starts by asking the agent: a fix, a feature, a pull
request on one of their GitHub repositories. Off until configured (docs/jobs.md). Two
runners exist, both Claude: Claude Code through the Agent SDK in the ``coder`` container
next to the agent (compose/coder.yml), on the user's Claude subscription
(intelligent_home_agent.jobs_claude_code); or a Managed Agents session, where Anthropic
runs the agent loop and a sandbox, billed to the API (intelligent_home_agent.jobs_managed).
Another coding agent is one more class with the same small protocol (``Backend``): start
a session, send it a message, interrupt it, and follow its events, reduced here to what
the coder said and when it stopped.

A request is never blocked by a job: ``start`` returns as soon as the session exists, and
a background task follows the session and reports each time the coder stops, with the pull
request's address once it has one, through whatever ``report`` is (main.report_job: the
channel the agent's own reports leave by, deliver.py). The user answers it through the agent
(``ask``), which queues their message in the session, and the same task brings the reply.

Jobs are kept on the data volume, so a restart picks up the sessions still open. Nobody
watches a run: a runner that pauses for an approval refuses it and tells the coder to
explain what it needs instead.

The agent picks the coder's model per job from the request's complexity: the cheapest for
the simplest tasks, the usual one for almost everything, the strongest when it is really
complex (``MODELS`` by default, the JOBS_MODEL_* settings).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from intelligent_home_agent.progress import Run, Tracker
from intelligent_home_agent.tools.base import ToolError

log = logging.getLogger(__name__)

RUNNING, IDLE, DONE, FAILED, STOPPED = "running", "idle", "done", "failed", "stopped"
OPEN = frozenset({RUNNING, IDLE})
SIMPLE, NORMAL, COMPLEX = "simple", "normal", "complex"
MODELS = {SIMPLE: "claude-sonnet-5-5", NORMAL: "claude-opus-5-5", COMPLEX: "claude-fable-5-1"}
# Until the tracker has timed some: how long a job runs before its first report.
JOB_SECONDS = {SIMPLE: 10 * 60, NORMAL: 25 * 60, COMPLEX: 45 * 60, "follow_up": 10 * 60}
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
MAX_REPORT_CHARS = 2500
RECONNECT_SECONDS = 15
# What the user gets when the runner no longer knows the session (its state was lost, or
# the job was started by the other runner): the job is closed rather than polled forever.
# main passes it in the user's language (i18n.job_gone).
GONE = "⚠️ The runner no longer knows this session: job closed. Start it again if needed."
# What the coder gets when it pauses for an approval nobody is there to give.
DENIED = (
    "Not approved: nobody watches this run. Find another way, or stop and tell the user "
    "exactly what you need and why."
)
BRIEF = """You work for {user}, through their assistant {assistant}, which relays this
chat. Nobody watches the run live: work on your own and stop only when you are done or
really blocked, then say in a few lines what you did, how you checked it and what is left.
{repo_lines}The user's request:

{request}"""
REPO_LINES = """The repository {repo} is cloned at {path}{branch}; git push on it is already
authenticated, and so is the GitHub API through the GH_TOKEN environment variable (gh, or
curl -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/repos/{repo}/pulls) to
open the pull request. Commit as {git_name} <{git_email}>.
"""
RUBRIC = """# Pull request rubric
- A branch was pushed to {repo} and a pull request opened against its default branch;
  the final message gives the pull request's URL.
- The change does what the request asks, nothing more, and nothing it asked is missing.
- The tests and linters the repository defines pass on the branch; new behaviour has tests.
- Branch name, commit messages, the pull request description and the language of what is
  written follow the repository's own conventions (CLAUDE.md, AGENTS.md, CONTRIBUTING).
- The diff holds no secret, token or key.
- The final message says what was done, how it was verified, and anything left open.
"""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionGone(ToolError):
    """The backend has no such session any more."""


@dataclass
class Event:
    """What a runner's events boil down to for the chat.

    ``said``: the coder wrote ``text``. ``stopped``: the coder stopped and waits (``status``
    IDLE, or FAILED when the run can't go on); ``ended``: the session is over for good
    (DONE). ``at`` is the runner's time of the event, so a restart reports only newer stops.
    """

    kind: str
    text: str = ""
    status: str = IDLE
    at: str = ""


class Backend(Protocol):
    """A way to run a coding session; one class per runner."""

    async def start(
        self,
        job_id: str,
        title: str,
        brief: str,
        rubric: str,
        repo: str,
        branch: str,
        model: str,
    ) -> str:
        """Start a session on ``brief`` and return its id. ``rubric`` says what done is for
        a repository job ("" without one); ``repo`` is "owner/name", "" for none; ``model``
        is the coder's model, when the runner lets a session choose it."""
        ...

    async def send(self, session: str, text: str) -> None: ...

    async def interrupt(self, session: str) -> None: ...

    def follow(self, session: str) -> AsyncIterator[Event]:
        """The session's events from the start, deduplicated by the backend, until the
        connection ends; the runner reconnects while the job is open. Raises SessionGone
        when the backend has no such session."""
        ...

    def console_url(self, session: str) -> str: ...


@dataclass
class Job:
    id: str
    title: str
    request: str
    session_id: str
    repo: str = ""
    status: str = RUNNING
    model: str = ""
    created_at: str = ""
    updated_at: str = ""
    # The last thing the coder said when it stopped, and the pull request once it has one.
    report: str = ""
    pr_url: str = ""
    # The runner's time of the last stop reported, so a restart reports only newer ones.
    reported_at: str = ""
    # The user asked to stop it: the next stop closes it.
    closing: bool = False
    # Started by the code, not the user (the nightly review): nobody waits for it, so its
    # first stop ends it.
    quiet: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "job": self.id,
            "title": self.title,
            "repo": self.repo or None,
            "status": self.status,
            "model": self.model or None,
            "pull_request": self.pr_url or None,
            "last_report": self.report or None,
            "updated_at": self.updated_at,
        }


class JobStore:
    def __init__(self, file: Path) -> None:
        self._file = file

    def all(self) -> list[Job]:
        try:
            data = json.loads(self._file.read_text())
        except (OSError, ValueError):
            return []
        known = {f.name for f in fields(Job)}
        return [Job(**{k: v for k, v in item.items() if k in known}) for item in data]

    def get(self, job_id: str) -> Job | None:
        return next((j for j in self.all() if j.id == job_id), None)

    def put(self, job: Job) -> None:
        job.updated_at = now()
        jobs = [j for j in self.all() if j.id != job.id] + [job]
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in jobs], ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(self._file)


Reporter = Callable[[Job, str], Awaitable[None]]


class JobRunner:
    def __init__(
        self,
        backend: Backend,
        store: JobStore,
        *,
        report: Reporter,
        # Where the backend clones a repository, with {job} and {name} (the repo's name).
        repo_path: str = "/workspace/{name}",
        git_name: str = "",
        git_email: str = "",
        user_name: str = "the user",
        assistant_name: str = "the assistant",
        gone: str = GONE,
        label: str = "Job {id}",
        models: dict[str, str] | None = None,
        reconnect_seconds: float = RECONNECT_SECONDS,
        tracker: Tracker | None = None,
    ) -> None:
        self._backend = backend
        self._tracker = tracker
        # The tracker's run of each job the user waits for, and what its coder last said.
        self._runs: dict[str, Run] = {}
        self._last_said: dict[str, str] = {}
        self._store = store
        self._report = report
        self._gone = gone
        self._label = label
        self._repo_path = repo_path
        self._models = MODELS | (models or {})
        self._git_name = git_name
        self._git_email = git_email
        self._user = user_name
        self._assistant = assistant_name
        self._reconnect_seconds = reconnect_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        # The jobs being followed: one object each, shared by the follower and the tools.
        self._live: dict[str, Job] = {}

    def console_url(self, job: Job) -> str:
        return self._backend.console_url(job.session_id)

    def list(self) -> list[Job]:
        jobs = [self._live.get(j.id, j) for j in self._store.all()]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job:
        job_id = job_id.strip().lstrip("#")
        job = self._live.get(job_id) or self._store.get(job_id)
        if job is None:
            raise ToolError(f"No job {job_id}: list_jobs gives the ones there are.")
        return job

    def brief(self, job_id: str, request: str, repo: str, branch: str) -> str:
        repo_lines = ""
        if repo:
            repo_lines = REPO_LINES.format(
                repo=repo,
                path=self._repo_path.format(job=job_id, name=repo.rsplit("/", 1)[-1]),
                branch=f" (branch {branch})" if branch else "",
                git_name=self._git_name or self._user,
                git_email=self._git_email or "",
            )
        return BRIEF.format(
            user=self._user, assistant=self._assistant, repo_lines=repo_lines, request=request
        )

    def model_for(self, complexity: str) -> str:
        try:
            return self._models[complexity or NORMAL]
        except KeyError:
            raise ToolError(f"complexity must be one of {', '.join(MODELS)}.") from None

    async def start(
        self,
        title: str,
        request: str,
        repo: str = "",
        branch: str = "",
        complexity: str = NORMAL,
        *,
        quiet: bool = False,
        announce: bool = False,
    ) -> Job:
        job_id = secrets.token_hex(2)
        rubric = RUBRIC.format(repo=repo) if repo else ""
        model = self.model_for(complexity)
        session = await self._backend.start(
            job_id,
            title[:200],
            self.brief(job_id, request, repo, branch),
            rubric,
            repo,
            branch,
            model,
        )
        job = Job(job_id, title, request, session, repo, model=model, created_at=now(), quiet=quiet)
        self._store.put(job)
        if announce and not quiet:
            self._track(job, f"job_{complexity or NORMAL}", JOB_SECONDS[complexity or NORMAL])
        self._follow_in_background(job)
        return job

    def _track(self, job: Job, kind: str, default: float) -> None:
        if self._tracker is None:
            return
        if old := self._runs.pop(job.id, None):
            old.end(ok=False)

        def said() -> str:
            last = " ".join(self._last_said.get(job.id, "").split())
            return f'"{last[:70]}{"…" if len(last) > 70 else ""}"' if last else ""

        self._runs[job.id] = self._tracker.begin(kind, self._label.format(id=job.id), default, said)

    def estimate_minutes(self, job: Job) -> int | None:
        run = self._runs.get(job.id)
        return run.estimate_minutes if run else None

    async def ask(self, job_id: str, text: str, *, announce: bool = False) -> Job:
        job = self.get(job_id)
        if job.status not in OPEN:
            raise ToolError(f"Job {job.id} is {job.status}: start a new one.")
        await self._backend.send(job.session_id, text)
        job.status = RUNNING
        self._store.put(job)
        if announce:
            self._track(job, "job_follow_up", JOB_SECONDS["follow_up"])
        if job.id not in self._tasks:
            self._follow_in_background(job)
        return job

    async def stop(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status not in OPEN:
            return job
        if job.status == RUNNING:
            job.closing = True
            self._store.put(job)
            await self._backend.interrupt(job.session_id)
            return job
        self._close(job, STOPPED)
        return job

    async def resume(self) -> None:
        """After a restart, follow again every session still open."""
        for job in self._store.all():
            if job.status in OPEN:
                self._follow_in_background(job)

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _follow_in_background(self, job: Job) -> None:
        self._live[job.id] = job
        self._tasks[job.id] = asyncio.create_task(self.follow(job))

    def _close(self, job: Job, status: str) -> None:
        job.status = status
        self._store.put(job)
        task = self._tasks.pop(job.id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def follow(self, job: Job) -> None:
        """Read the session's events until the job is closed, reconnecting when the
        connection drops: the backend replays from the start and deduplicates, and stops
        already reported are told apart by their time."""
        said: list[str] = []
        while job.status in OPEN:
            try:
                async for event in self._backend.follow(job.session_id):
                    await self._handle(job, event, said)
                    if job.status not in OPEN:
                        break
            except asyncio.CancelledError:
                raise
            except SessionGone:
                log.warning("Job %s: session %s is gone, closing", job.id, job.session_id)
                await self._stopped(job, STOPPED, [self._gone], now())
            except Exception:
                log.warning("Job %s: connection lost", job.id, exc_info=True)
            if job.status in OPEN:
                await asyncio.sleep(self._reconnect_seconds)
        self._tasks.pop(job.id, None)

    async def _handle(self, job: Job, event: Event, said: list[str]) -> None:
        if event.kind == "said":
            if event.text.strip():
                said.append(event.text.strip())
                self._last_said[job.id] = event.text.strip().splitlines()[-1]
            if job.status == IDLE:
                job.status = RUNNING
                self._store.put(job)
        elif event.kind == "stopped":
            status = STOPPED if job.closing else event.status
            if job.quiet and status == IDLE:
                status = DONE
            await self._stopped(job, status, said, event.at)
        elif event.kind == "ended":
            status = job.status if job.status in (FAILED, STOPPED) else DONE
            await self._stopped(job, status, said, event.at)

    async def _stopped(self, job: Job, status: str, said: list[str], at: str) -> None:
        text = "\n".join(said).strip()
        said.clear()
        # It stopped to report or to wait for the user: what they waited for is over.
        self._last_said.pop(job.id, None)
        if run := self._runs.pop(job.id, None):
            run.end(ok=status != FAILED)
        for url in PR_URL.findall(text):
            job.pr_url = url
        if len(text) > MAX_REPORT_CHARS:
            text = "…" + text[-MAX_REPORT_CHARS:]
        new = not at or at > job.reported_at
        if text and new:
            job.report = text
        job.reported_at = max(job.reported_at, at)
        if status in OPEN:
            job.status = status
            self._store.put(job)
        else:
            self._close(job, status)
        if new:
            try:
                await self._report(job, text)
            except Exception:
                log.exception("Job %s: could not report", job.id)
