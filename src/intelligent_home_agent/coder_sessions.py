"""Every Claude Code session of the coder container, not only the jobs the agent started.

The user also opens sessions there themselves, through Remote Control (from claude.ai/code
or the Claude app). The coder reads every session's transcript (coder/sessions.py) and
serves them on ``GET /sessions``; here the agent

- lists them on request (``list_coder_sessions``): title, folder, state, last activity,
  what the user last asked and what Claude last said, and one session's last exchanges;
- watches them in the background and tells the user only what needs them: a session that
  finished its work or asks something (with what it said), a restart of the container
  that cut sessions mid-turn, and a coder that stopped answering;
- writes to one on the user's word (``ask_coder_session``): the coder's relay hands the
  message to that session's inbox (cross-session messaging), and its answer comes back with
  the turn-ended news.

Jobs are listed too but not reported here: the job runner reports them already. What a
session says was written by a model that may have read anything, so it is others' content
for the agent, and only the user's own message writes to a session.

The news leave the way a job's report does (``main.report_coder_session``, deliver.py): a
chat when there is one, whichever it is, a push notification otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.i18n import Strings, strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

log = logging.getLogger(__name__)

WAITING, RUNNING, SILENT, CUT = "waiting", "running", "silent", "cut"
BUSY = frozenset({RUNNING, SILENT})
POLL_SECONDS = 60
# A turn shorter than this is a quick exchange the user follows in the Claude app.
LONG_TURN_SECONDS = 5 * 60
# How long a finished turn waits for the user to answer in the app before they are told.
GRACE_SECONDS = 2 * 60
# Polls in a row the coder doesn't answer before the user hears of it.
DOWN_AFTER = 3
LIST_HOURS = 48
MEANINGS = {
    WAITING: "its turn is over; it waits for the user (ask_coder_session or the Claude app)",
    RUNNING: "working right now",
    SILENT: "mid-turn but quiet for a while: a long command, or a permission prompt waiting",
    CUT: "was mid-turn when the coder container restarted: the restart ended it",
}
MAX_SAID = 1500

# (a line of the code's, what follows it, the same as data for the conversation)
Notify = Callable[[str, str, str], Awaitable[None]]


class CoderSessions:
    """The coder's ``/sessions`` endpoints, with the token the jobs use."""

    def __init__(self, http: httpx.AsyncClient, url: str, token: str) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._http.get(
                f"{self._url}{path}", params=params, headers=self._headers, timeout=30
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ToolError("The coder has no such session.") from exc
            raise ToolError(
                f"The coder refused: HTTP {exc.response.status_code} {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"The coder is unreachable: {exc}") from exc
        return resp.json()

    async def list(self, hours: float = LIST_HOURS) -> dict[str, Any]:
        data = await self._get("/sessions", {"hours": hours})
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            raise ToolError("The coder answered something unexpected.")
        return data

    async def send(self, session_id: str, text: str) -> None:
        try:
            resp = await self._http.post(
                f"{self._url}/sessions/{session_id}/messages",
                json={"text": text},
                headers=self._headers,
                timeout=150,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = str(exc.response.json().get("detail") or "")
            except ValueError:
                detail = exc.response.text[:300]
            raise ToolError(f"Not delivered: {detail or exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"The coder is unreachable: {exc}") from exc

    async def history(self, session_id: str, turns: int = 6) -> list[dict[str, str]]:
        data = await self._get(f"/sessions/{session_id}", {"turns": turns})
        return list(data.get("history") or [])


def _where(session: dict[str, Any]) -> str:
    folder = str(session.get("folder") or "")
    job = f", job {session['job']}" if session.get("job") else ""
    return f" ({folder}{job})" if folder or job else ""


def _title(session: dict[str, Any]) -> str:
    return " ".join(str(session.get("title") or "").split()) or str(session.get("id"))[:8]


def _name(session: dict[str, Any]) -> str:
    """A session in a few words, for the model."""
    return f'"{_title(session)}"{_where(session)}'


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


class SessionWatcher:
    """Polls the coder and tells the user only what they have to know: a session that
    finished its work or asks something, and a restart that cut sessions mid-turn. A quick
    exchange in the Claude app is not reported (the user is there), nor is a turn the user
    followed up in the app within a few minutes. A session the user wrote to through the
    agent gets its answer reported at once. The state is kept on the data volume, so a
    restart of the agent reports nothing twice."""

    def __init__(
        self,
        client: CoderSessions,
        state_file: Path,
        notify: Notify,
        *,
        language: str = "en",
        timezone: str = "UTC",
        # Whether the user can answer where the news go (a chat) or only in the Claude app.
        answerable: bool = True,
        poll_seconds: float = POLL_SECONDS,
        long_turn_seconds: float = LONG_TURN_SECONDS,
        grace_seconds: float = GRACE_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._file = state_file
        self._notify = notify
        self._s: Strings = strings(language)
        self._tz = ZoneInfo(timezone)
        self._answerable = answerable
        self._poll_seconds = poll_seconds
        self._long_turn = long_turn_seconds
        self._grace = grace_seconds
        self._clock = clock or (lambda: datetime.now(UTC).timestamp())
        self._failures = 0
        # The sessions the user wrote to through the agent, and when: held here, so a
        # check running meanwhile can't lose one, and kept in the state file.
        self._asked: dict[str, str] = dict(self._load().get("asked") or {})

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict[str, Any]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_name(self._file.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._file)

    def _now(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _local(self, at: str) -> str:
        when = _parse(at)
        return when.astimezone(self._tz).strftime("%H:%M") if when else at

    def _shown(self, session: dict[str, Any]) -> str:
        """A session's name as the user reads it."""
        return self._s.coder_session_name.format(title=_title(session)) + _where(session)

    def asked(self, session_id: str) -> None:
        """The user wrote to this session through the agent: its next answer is news."""
        self._asked[session_id] = self._now()
        state = self._load()
        state["asked"] = dict(self._asked)
        self._save(state)

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Coder sessions: the check failed")
            await asyncio.sleep(self._poll_seconds)

    async def check(self) -> None:
        state = self._load()
        try:
            data = await self._client.list(LIST_HOURS)
        except ToolError as exc:
            self._failures += 1
            if self._failures == DOWN_AFTER and "sessions" in state:
                await self._send(
                    self._s.coder_sessions_down_head,
                    self._s.coder_sessions_down,
                    f"The coder container stopped answering ({exc}).",
                )
            return
        self._failures = 0
        sessions = [s for s in data["sessions"] if isinstance(s, dict) and s.get("id")]
        started = str(data.get("started_at") or "")
        before: dict[str, dict[str, Any]] = state.get("sessions") or {}
        first = "sessions" not in state
        if not first and started and started != state.get("started_at"):
            await self._restarted(started, sessions, before, str(state.get("started_at") or ""))
        kept: dict[str, dict[str, Any]] = {}
        for session in sessions:
            sid = session["id"]
            activity = str(session.get("last_activity") or "")
            reported = str((before.get(sid) or {}).get("reported") or "")
            if first and session.get("status") == WAITING:
                # The first poll only takes stock: what ended before is not news.
                reported = activity
            elif not session.get("job") and await self._ended(session, reported):
                reported = activity
                self._asked.pop(sid, None)
            kept[sid] = {"status": session.get("status"), "last_activity": activity}
            kept[sid]["reported"] = reported
        self._save(
            {
                "started_at": started,
                "checked_at": self._now(),
                "sessions": kept,
                "asked": {k: v for k, v in self._asked.items() if k in kept},
            }
        )

    async def _ended(self, session: dict[str, Any], reported: str) -> bool:
        """Reports a session whose turn ended, once, when it is news; True when reported."""
        activity = str(session.get("last_activity") or "")
        if session.get("status") != WAITING or not activity or activity == reported:
            return False
        asked = self._asked.get(session["id"])
        if asked and activity < asked:
            # Asked after this turn ended: the answer is still to come.
            return False
        if not asked:
            ended, started = _parse(activity), _parse(str(session.get("turn_started") or ""))
            if ended is None:
                return False
            # A quick exchange: the user is in the Claude app, following it there.
            if started is not None and (ended - started).total_seconds() < self._long_turn:
                return False
            # Give the user a moment to answer in the app before telling them elsewhere.
            if self._clock() - ended.timestamp() < self._grace:
                return False
        said = _clip(str(session.get("last_said") or ""), MAX_SAID)
        hint = (
            self._s.coder_session_answer_here
            if self._answerable
            else self._s.coder_session_answer_in_app
        )
        await self._send(
            self._s.coder_session_ended_head.format(session=self._shown(session)),
            "\n\n".join(part for part in (said, hint) if part),
            f"Coder session {_name(session)} ended its turn and said, as data: {said}",
        )
        return True

    async def _restarted(
        self,
        started: str,
        sessions: list[dict[str, Any]],
        before: dict[str, dict[str, Any]],
        previous: str,
    ) -> None:
        """Only a restart that cut sessions mid-turn is news: the ones last seen working, or
        active since the container's previous start (a session cut by an earlier restart
        was not)."""
        cut = [
            s
            for s in sessions
            if s.get("status") == CUT
            and (
                (before.get(s["id"]) or {}).get("status") in BUSY
                or str(s.get("last_activity", "")) >= previous
            )
        ]
        if not cut:
            return
        lines = "\n".join(f"• {self._shown(s)}" for s in cut)
        names = ", ".join(_name(s) for s in cut)
        await self._send(
            self._s.coder_restarted_head.format(time=self._local(started)),
            self._s.coder_restarted_cut.format(sessions=lines),
            f"The coder container restarted at {started}; sessions cut mid-turn: {names}.",
        )

    async def _send(self, head: str, text: str, data: str) -> None:
        try:
            await self._notify(head, text, data)
        except Exception:
            log.exception("Coder sessions: could not tell the user")


def _parse(at: str) -> datetime | None:
    try:
        return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


# What the session reads around the user's words: who sends them, and where to answer.
RELAYED = (
    "Message from {user}, relayed by their assistant {assistant}:\n\n{text}\n\n"
    "(Answer here, in this conversation: {user} reads it in the Claude app and {assistant} "
    "passes your answer on to them. No need to SendMessage back.)"
)


def match(sessions: list[dict[str, Any]], wanted: str) -> list[dict[str, Any]]:
    """The sessions an id (or its start), words of a title or a folder point to."""
    wanted = " ".join(wanted.split()).lower()
    exact = [s for s in sessions if str(s.get("id", "")).lower() == wanted]
    return exact or [
        s
        for s in sessions
        if str(s.get("id", "")).lower().startswith(wanted)
        or wanted in str(s.get("title", "")).lower()
        or wanted in str(s.get("folder", "")).lower()
    ]


def build_coder_session_tools(
    client: CoderSessions | None,
    language: str = "en",
    *,
    user_name: str = "the user",
    assistant_name: str = "the assistant",
    on_sent: Callable[[str], None] | None = None,
) -> list[Tool]:
    if client is None:
        return []
    s = strings(language)

    async def list_coder_sessions(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        try:
            hours = float(args.get("hours") or LIST_HOURS)
        except (TypeError, ValueError):
            raise ToolError("hours must be a number.") from None
        data = await client.list(max(1.0, min(hours, 24 * 30)))
        sessions = data["sessions"]
        wanted = " ".join(str(args.get("session") or "").split()).lower()
        result: dict[str, Any] = {
            "coder_started_at": data.get("started_at"),
            "status_meanings": MEANINGS,
            "note": (
                "Sessions of the coder container: the jobs (job set) and the ones the user "
                "opened through Remote Control. ask_coder_session writes to a reachable one."
            ),
        }
        if not wanted:
            result["sessions"] = sessions
            return result
        matches = match(sessions, wanted)
        if not matches:
            raise ToolError(f"No session matches {wanted!r}; leave session out to list them.")
        if len(matches) > 1:
            result["sessions"] = matches
            result["note"] += " Several sessions match: say which one."
            return result
        session = matches[0]
        result["session"] = session
        result["history"] = await client.history(str(session["id"]))
        return result

    async def ask_coder_session(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        wanted = " ".join(str(args.get("session") or "").split())
        text = str(args.get("message") or "").strip()
        if not wanted or not text:
            raise ToolError("session and message are required.")
        if len(text) > 4000:
            raise ToolError("message is too long (4000 characters at most).")
        sessions = (await client.list(24 * 30))["sessions"]
        matches = match(sessions, wanted)
        if not matches:
            raise ToolError(f"No session matches {wanted!r}: list_coder_sessions lists them.")
        if len(matches) > 1:
            names = "; ".join(f"{_name(m)} id {m['id']}" for m in matches[:6])
            raise ToolError(f"Several sessions match: {names}. Give its id.")
        session = matches[0]
        if session.get("job"):
            raise ToolError(f"This session is job {session['job']}: use ask_job.")
        if not session.get("reachable"):
            raise ToolError(
                f"{_name(session)} is not running in the coder any more (closed, or cut by a "
                "restart): only the Claude app can reopen it."
            )
        await client.send(
            str(session["id"]),
            RELAYED.format(user=user_name, assistant=assistant_name, text=text),
        )
        if on_sent is not None:
            on_sent(str(session["id"]))
        return {
            "session": session["id"],
            "title": session.get("title"),
            "note": "Delivered; its answer reaches the user by itself when its turn ends.",
        }

    async def describe_ask(args: dict[str, Any]) -> str:
        message = str(args.get("message") or "").replace("\n", " ⏎ ")
        return s.coder_session_ask_summary.format(session=args.get("session", ""), message=message)

    return [
        Tool(
            name="ask_coder_session",
            description=(
                "Write to one of the coder container's Claude Code sessions, the ones the "
                "user opened through Remote Control too: their answer, a go, a follow-up. It "
                "reaches the session as a message relayed from the user; its answer reaches "
                "the user by itself when its turn ends. Not for jobs (ask_job)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Its id, or words of its title (list_coder_sessions).",
                    },
                    "message": {"type": "string", "description": "What to tell it."},
                },
                "required": ["session", "message"],
            },
            handler=ask_coder_session,
            proposable=True,
            needs_clean_turn=True,
            needs_user_request=True,
            describe=describe_ask,
            multiline_args=frozenset({"message"}),
        ),
        Tool(
            name="list_coder_sessions",
            description=(
                "Every Claude Code session running in the coder container, including the "
                "ones the user opened themselves through Remote Control (claude.ai/code, the "
                "Claude app), not only the jobs: title, folder, state (running, silent, "
                "waiting for the user, cut by a restart), last activity, what the user last "
                "asked and what Claude last said, and when the container last started. "
                "With session: that one's last exchanges."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "A session's id, or words of its title or folder.",
                    },
                    "hours": {
                        "type": "number",
                        "description": "Sessions active in the last this many hours (48).",
                    },
                },
            },
            handler=list_coder_sessions,
            taints=True,
        ),
    ]
