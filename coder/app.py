"""The coder: runs the agent's coding jobs with Claude Code, through the Agent SDK, on the
user's Claude subscription (docs/jobs.md).

A small HTTP service in its own container (coder/Dockerfile), reached by the agent on an
internal network only. One job is one Claude Code session in its own folder under /work,
where the repository is cloned first; a follow-up resumes the same session once the current
turn is over, and an interrupt ends the turn. Every event is numbered and kept in a file per
job, so the agent reads them from any point and a restart loses nothing already written.

Secrets live here and nowhere else in the stack: the Claude login (CLAUDE_CODE_OAUTH_TOKEN,
from `claude setup-token`, valid a year) and the GitHub token (GH_TOKEN, read by git through
gh and by the GitHub API). ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN must not be set here:
they outrank the login, and Claude Code would bill the API instead of the subscription.
Claude Code's transcripts go to CLAUDE_CONFIG_DIR, on the /work volume, so a session can be
resumed after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from coder import sessions as claude_sessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("coder")

RUNNING, IDLE, FAILED, STOPPED = "running", "idle", "failed", "stopped"
# When this service started: a session mid-turn before it was cut by the restart.
BOOTED = time.time()
WORK = Path(os.environ.get("CODER_WORK", "/work"))
TOKEN = os.environ.get("CODER_TOKEN", "")
# The house rules appended to Claude Code's own system prompt: the body of agents/coder.md.
RULES_FILE = Path(os.environ.get("CODER_RULES", "/app/agents/coder.md"))
# A runaway guard on Claude Code's own cost estimate, notional on a subscription.
MAX_BUDGET_USD = float(os.environ.get("CODER_MAX_BUDGET_USD", "15"))
# The largest single message the SDK reads from Claude Code. Its 1 MB default ends the turn
# on one big tool result (a full test run, a lockfile read).
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# Tools the coder may use without asking; nobody is there to answer a prompt, and in
# "dontAsk" mode anything else is denied rather than asked.
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "TodoWrite"]
# The relay: a short Claude Code turn that hands the user's message to another session of
# this container through cross-session messaging (SendMessage, to the session's inbox).
RELAY_MODEL = os.environ.get("CODER_RELAY_MODEL", "claude-sonnet-5-5")
RELAY_TOOLS = ["SendMessage", "ToolSearch"]
RELAY_SECONDS = 120
RELAY_PROMPT = """Your only job: deliver a message to another Claude Code session, unchanged.
Call SendMessage once, with "to" set to exactly {address!r} and "message" set to exactly the
text between the markers below, character for character: no summary, no translation, nothing
added. Do nothing else, then answer with one word: SENT, or FAILED and why.

<<<MESSAGE
{text}
MESSAGE>>>"""
# The few words this service adds to the coder's own, in the user's language.
TEXTS = {
    "en": {
        "restarted": "⚠️ The coder restarted during this turn.",
        "interrupted": "Interrupted.",
        "failed": "⚠️ The coder could not go on: {error}",
    },
    "fr": {
        "restarted": "⚠️ Le coder a redémarré pendant ce tour.",
        "interrupted": "Interrompu.",
        "failed": "⚠️ Le coder n'a pas pu continuer : {error}",
    },
}
SAYS = TEXTS.get(os.environ.get("CODER_LANGUAGE", "en")[:2].lower(), TEXTS["en"])
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_BRANCH = re.compile(r"^[\w./-]+$")
_ID = re.compile(r"^[a-z0-9]{1,16}$")
_MODEL = re.compile(r"^[\w.-]{0,64}$")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class JobState:
    id: str
    title: str
    repo: str = ""
    branch: str = ""
    cwd: str = ""
    status: str = RUNNING
    # The model the agent chose for this job; "" leaves Claude Code its default.
    model: str = ""
    # Claude Code's own session id, to resume the conversation turn after turn.
    session_id: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    # Messages the user sent while a turn ran: together, they become the next turn.
    pending: list[str] = field(default_factory=list)
    # The user asked to stop the current turn: what was pending is dropped with it.
    interrupted: bool = False

    def add(self, kind: str, text: str = "", status: str = "") -> None:
        self.events.append(
            {"n": len(self.events) + 1, "kind": kind, "text": text, "status": status, "at": now()}
        )


class Jobs:
    """The jobs, in memory and in one JSON file each under WORK/jobs."""

    def __init__(self, root: Path) -> None:
        self._dir = root / "jobs"
        self._items: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # The Claude Code client of each running turn, to interrupt it cleanly.
        self._clients: dict[str, Any] = {}
        self._dir.mkdir(parents=True, exist_ok=True)
        for file in self._dir.glob("*.json"):
            try:
                data = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            job = JobState(**data)
            # A turn that was running when the service stopped is over, without a word.
            if job.status == RUNNING:
                job.status = IDLE
                job.add("stopped", SAYS["restarted"], IDLE)
                self.save(job)
            self._items[job.id] = job

    def get(self, job_id: str) -> JobState:
        job = self._items.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
        return job

    def put(self, job: JobState) -> None:
        self._items[job.id] = job
        self.save(job)

    def save(self, job: JobState) -> None:
        tmp = self._dir / f"{job.id}.tmp"
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False))
        tmp.replace(self._dir / f"{job.id}.json")

    def running(self, job: JobState) -> bool:
        task = self._tasks.get(job.id)
        return task is not None and not task.done()

    def start_turn(self, job: JobState, prompt: str) -> None:
        job.status = RUNNING
        self.save(job)
        self._tasks[job.id] = asyncio.create_task(self._turn(job, prompt))

    async def interrupt(self, job: JobState) -> None:
        """Ends the turn: through Claude Code's own interrupt when the client is up, which
        still delivers the turn's result, else by cancelling the task."""
        task = self._tasks.get(job.id)
        if task is None or task.done():
            return
        job.interrupted = True
        client = self._clients.get(job.id)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.interrupt()
                return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _turn(self, job: JobState, prompt: str) -> None:
        def keep(client: Any) -> None:
            self._clients[job.id] = client

        try:
            async for item in run_turn(prompt, job.cwd, job.session_id, keep, model=job.model):
                if item["kind"] == "session":
                    job.session_id = item["text"]
                elif item["kind"] in ("said", "tool"):
                    job.add(item["kind"], item["text"])
                elif item["kind"] == "result":
                    job.status = FAILED if item.get("is_error") else IDLE
                    if item.get("is_error") and item["text"]:
                        job.add("said", f"⚠️ {item['text']}")
                self.save(job)
        except asyncio.CancelledError:
            self._clients.pop(job.id, None)
            job.status = IDLE
            job.add("said", SAYS["interrupted"])
            job.add("stopped", status=IDLE)
            self.save(job)
            raise
        except Exception as exc:
            log.exception("Job %s: turn failed", job.id)
            job.status = FAILED
            job.add("said", SAYS["failed"].format(error=exc))
        else:
            if job.status == RUNNING:
                job.status = IDLE
        finally:
            self._clients.pop(job.id, None)
        if job.interrupted:
            job.interrupted = False
            job.pending.clear()
        if job.status == IDLE and job.pending:
            next_prompt = "\n\n".join(job.pending)
            job.pending.clear()
            self.save(job)
            self.start_turn(job, next_prompt)
            return
        job.add("stopped", status=job.status)
        self.save(job)


def house_rules(path: Path = RULES_FILE) -> str:
    """The body of agents/coder.md, without its front matter; "" when the file is missing."""
    try:
        text = path.read_text()
    except OSError:
        return ""
    if text.startswith("---\n") and "\n---\n" in text[4:]:
        text = text[text.index("\n---\n", 4) + 5 :]
    return text.strip()


async def run_turn(
    prompt: str, cwd: str, resume: str, keep: Any = None, *, model: str = ""
) -> AsyncIterator[dict[str, Any]]:
    """One Claude Code turn, as plain events: the session id, what it said, the tools it
    used, and the result. ``keep`` receives the client, so an interrupt can reach it. The
    SDK is imported here so the HTTP layer can be tested without it."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
    )

    options = ClaudeAgentOptions(
        cwd=cwd or str(WORK),
        permission_mode="dontAsk",
        allowed_tools=ALLOWED_TOOLS,
        # The repository's CLAUDE.md and settings; nothing from a user profile.
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": house_rules()},
        max_budget_usd=MAX_BUDGET_USD,
        max_buffer_size=MAX_MESSAGE_BYTES,
        model=model or None,
        resume=resume or None,
    )
    async with ClaudeSDKClient(options) as client:
        if keep is not None:
            keep(client)
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, SystemMessage):
                if message.subtype == "init" and (sid := message.data.get("session_id")):
                    yield {"kind": "session", "text": sid}
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"kind": "said", "text": block.text.strip()}
                    elif isinstance(block, ToolUseBlock):
                        yield {"kind": "tool", "text": block.name}
            elif isinstance(message, ResultMessage):
                if message.session_id:
                    yield {"kind": "session", "text": message.session_id}
                yield {
                    "kind": "result",
                    "text": message.result or "",
                    "is_error": bool(message.is_error),
                }


def _succeeded(content: Any) -> bool:
    """SendMessage's result, a JSON object as text (alone or in text blocks), says success."""
    texts = [content] if isinstance(content, str) else []
    if isinstance(content, list):
        texts = [str(b.get("text") or "") for b in content if isinstance(b, dict)]
    for text in texts:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("success") is True:
            return True
    return False


async def run_relay(address: str, text: str) -> tuple[bool, str]:
    """Hands ``text`` to the session whose inbox is ``address``; (delivered, detail). It
    counts as delivered only when SendMessage itself was called with that address and that
    exact text and said it succeeded, whatever the relay's model claims."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    folder = WORK / "relay"
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    options = ClaudeAgentOptions(
        cwd=str(folder),
        permission_mode="dontAsk",
        allowed_tools=RELAY_TOOLS,
        setting_sources=[],
        model=RELAY_MODEL or None,
        max_turns=6,
    )
    ours: set[str] = set()
    detail = "SendMessage was not called."
    async with ClaudeSDKClient(options) as client:
        await client.query(RELAY_PROMPT.format(address=address, text=text))
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock) and block.name == "SendMessage":
                        given = block.input or {}
                        if given.get("to") == address and given.get("message") == text:
                            ours.add(block.id)
                        else:
                            detail = "The relay changed the address or the text; not counted."
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in ours:
                        if _succeeded(block.content):
                            return True, "Delivered to the session's inbox."
                        detail = f"SendMessage failed: {str(block.content)[:300]}"
    return False, detail


async def clone(repo: str, branch: str, into: Path) -> Path:
    """The repository, cloned through gh's credential helper (GH_TOKEN), on ``branch``."""
    await asyncio.to_thread(into.mkdir, parents=True, exist_ok=True)
    target = into / repo.rsplit("/", 1)[-1]
    args = ["git", "clone", "--quiet"]
    if branch:
        args += ["--branch", branch]
    args += [f"https://github.com/{repo}.git", str(target)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        await asyncio.to_thread(shutil.rmtree, into, ignore_errors=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"git clone failed: {out.decode(errors='replace')[-500:]}"
        )
    return target


app = FastAPI(title="intelligent-home-agent coder")
_bearer = HTTPBearer(auto_error=False)


def require_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not TOKEN or creds is None or not secrets.compare_digest(creds.credentials, TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")


def jobs_of(request: Request) -> Jobs:
    if not hasattr(request.app.state, "jobs"):
        request.app.state.jobs = Jobs(WORK)
    return request.app.state.jobs


class StartRequest(BaseModel):
    id: str = Field(pattern=_ID.pattern)
    title: str = Field(max_length=200)
    # The nightly review's brief carries a day of the agent's journal.
    brief: str = Field(min_length=1, max_length=100_000)
    repo: str = ""
    branch: str = ""
    model: str = Field("", pattern=_MODEL.pattern)


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)


def view(job: JobState, after: int = 0) -> dict[str, Any]:
    return {
        "job": job.id,
        "status": job.status,
        "session": job.session_id,
        "model": job.model,
        "events": [e for e in job.events if e["n"] > after],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sessions", dependencies=[Depends(require_token)])
async def list_sessions(request: Request, hours: float = 48) -> dict[str, Any]:
    """Every Claude Code session of the container written to in the last ``hours``: the
    jobs and the ones the user opened through Remote Control (coder/sessions.py)."""
    jobs = jobs_of(request)
    by_session = {j.session_id: j.id for j in jobs._items.values() if j.session_id}
    found = await asyncio.to_thread(
        claude_sessions.sessions,
        BOOTED,
        time.time(),
        within_seconds=max(0.0, min(hours, 24 * 30)) * 3600,
        jobs=by_session,
    )
    return {"started_at": claude_sessions.iso(BOOTED), "sessions": found}


@app.get("/sessions/{session_id}", dependencies=[Depends(require_token)])
async def session_history(session_id: str, turns: int = 6) -> dict[str, Any]:
    if not re.fullmatch(r"[\w-]{1,64}", session_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad session id")
    found = await asyncio.to_thread(
        claude_sessions.history, session_id, turns=max(1, min(turns, 20))
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")
    return {"session": session_id, "history": found}


class SessionMessage(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)


_relaying = asyncio.Lock()


@app.post("/sessions/{session_id}/messages", dependencies=[Depends(require_token)])
async def message_session(session_id: str, body: SessionMessage) -> dict[str, Any]:
    """Writes to a running session of this container, the user's Remote Control ones too,
    through the relay. Its answer is in its transcript, which GET /sessions reads."""
    if not re.fullmatch(r"[\w-]{1,64}", session_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad session id")
    address = (await asyncio.to_thread(claude_sessions.inboxes)).get(session_id)
    if address is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This session is not running here with an inbox (closed, or cut by a restart): "
            "it can only be reopened from the Claude app.",
        )
    async with _relaying:
        try:
            sent, detail = await asyncio.wait_for(
                run_relay(f"uds:{address}", body.text), RELAY_SECONDS
            )
        except TimeoutError:
            sent, detail = False, "The relay took too long."
        except Exception as exc:
            log.exception("Relay to %s failed", session_id)
            sent, detail = False, f"The relay failed: {exc}"
    if not sent:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail)
    return {"session": session_id, "sent": True, "detail": detail}


@app.post("/jobs", dependencies=[Depends(require_token)])
async def start_job(body: StartRequest, request: Request) -> dict[str, Any]:
    jobs = jobs_of(request)
    if body.id in jobs._items:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job already exists")
    if body.repo and not _REPO.match(body.repo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'repo must look like "owner/name"')
    if body.branch and not _BRANCH.match(body.branch):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad branch name")
    folder = WORK / body.id
    job = JobState(body.id, body.title, body.repo, body.branch, cwd=str(folder), model=body.model)
    if body.repo:
        job.cwd = str(await clone(body.repo, body.branch, folder))
    else:
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    jobs.put(job)
    jobs.start_turn(job, body.brief)
    return {"session": job.id, "status": job.status}


@app.get("/jobs/{job_id}/events", dependencies=[Depends(require_token)])
async def job_events(job_id: str, request: Request, after: int = 0) -> dict[str, Any]:
    return view(jobs_of(request).get(job_id), after)


@app.post("/jobs/{job_id}/messages", dependencies=[Depends(require_token)])
async def send_message(job_id: str, body: MessageRequest, request: Request) -> dict[str, Any]:
    jobs = jobs_of(request)
    job = jobs.get(job_id)
    if jobs.running(job):
        job.pending.append(body.text)
        jobs.save(job)
    else:
        jobs.start_turn(job, body.text)
    return {"status": job.status}


@app.post("/jobs/{job_id}/interrupt", dependencies=[Depends(require_token)])
async def interrupt_job(job_id: str, request: Request) -> dict[str, Any]:
    jobs = jobs_of(request)
    job = jobs.get(job_id)
    job.pending.clear()
    await jobs.interrupt(job)
    return {"status": job.status}
