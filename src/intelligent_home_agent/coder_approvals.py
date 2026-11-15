"""Claude Code's permission prompts in the coder, asked to the user through the agent.

The coder's Remote Control sessions run a PermissionRequest hook (coder/permission_hook.py)
that posts the request here and waits. Where the user can answer the agent's own reports
(a channel with a conversation, deliver.py), the request is sent there as a write waiting
for their go, like any other (conversations.Proposal): their yes runs
``allow_coder_permission``, which becomes the hook's decision; their no,
``refuse_coder_permission``. Through a one-way channel (a push notification) the user only
hears that a prompt waits. Without an answer in time the hook decides nothing, and the
prompt stays open in the Claude app, where Remote Control shows it anyway.

What the request shows (a command, a file path) is written by the coder's model, which may
have read anything: it is shown to the user as a question, never acted on here. A request
that can't be shown whole (too long, several lines) is not asked here: the Claude app shows
it in full, and the user only gets a word that it waits there (once a minute at most). A
few requests at a time at most, so a session can't flood the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Any

from intelligent_home_agent.i18n import Strings, strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

log = logging.getLogger(__name__)

WAIT_SECONDS = 240
MAX_DETAIL = 400
MAX_OPEN = 3
# A prompt the user can't answer here gets a word, at most this often.
NOTICE_SECONDS = 60
DENIED = "Refused by the user."

# (request id, question, summary) -> whether it was asked where the user can answer.
AskUser = Callable[[str, str, str], Awaitable[bool]]
Tell = Callable[[str], Awaitable[Any]]


def _where(request: dict[str, Any], s: Strings) -> str:
    folder = PurePosixPath(str(request.get("cwd") or "")).name[:60]
    return s.coder_permission_where.format(folder=folder) if folder else ""


def describe(request: dict[str, Any], s: Strings | None = None) -> tuple[str, str] | None:
    """The question and its one-line summary (the write waiting for the user's go), or None
    when the request can't be shown whole."""
    s = s or strings("en")
    tool = str(request.get("tool_name") or "?")[:40]
    given = request.get("tool_input") or {}
    if not isinstance(given, dict):
        return None
    if tool == "Bash":
        detail = str(given.get("command") or "")
    elif "file_path" in given:
        detail = str(given["file_path"])
    elif "url" in given:
        detail = str(given["url"])
    else:
        detail = json.dumps(given, ensure_ascii=False) if given else ""
    detail = detail.strip()
    if len(detail) > MAX_DETAIL or "\n" in detail or "\r" in detail:
        return None
    where = _where(request, s)
    head = s.coder_permission_question.format(where=where, tool=tool)
    summary = s.coder_permission_summary.format(where=where, tool=tool, detail=detail).strip()
    return (f"{head}\n{detail}" if detail else head), summary


def decision(allow: bool) -> dict[str, Any]:
    chosen: dict[str, Any] = {"behavior": "allow"}
    if not allow:
        chosen = {"behavior": "deny", "message": DENIED}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": chosen}}


class Approvals:
    def __init__(
        self,
        ask_user: AskUser | None = None,
        tell: Tell | None = None,
        language: str = "en",
        wait_seconds: float = WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        # Set by main once the app is up: how a request reaches the user (main.py,
        # ask_coder_permission) and how a plain word does.
        self.ask_user = ask_user
        self.tell = tell
        self._texts = strings(language)
        self._wait = wait_seconds
        self._clock = clock
        self._last_notice = -NOTICE_SECONDS
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._summaries: dict[str, str] = {}
        # Asks under way, counted before the request is sent so concurrent ones can't all
        # pass.
        self._open = 0

    async def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        """The hook's answer: a decision, or {} to leave the prompt to the Claude app."""
        shown = describe(request, self._texts)
        if shown is None or self.ask_user is None:
            await self._notice(request)
            return {}
        if self._open >= MAX_OPEN:
            return {}
        self._open += 1
        try:
            return await self._ask(request, *shown)
        finally:
            self._open -= 1

    async def _ask(self, request: dict[str, Any], question: str, summary: str) -> dict[str, Any]:
        request_id = secrets.token_hex(3)
        answered: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = answered
        self._summaries[request_id] = summary
        try:
            try:
                asked = await self.ask_user(request_id, question, summary)
            except Exception:
                log.warning("Could not ask the user a coder permission", exc_info=True)
                return {}
            if not asked:
                # Nowhere the user can answer: a word that it waits in the Claude app.
                await self._notice(request)
                return {}
            try:
                allow = await asyncio.wait_for(answered, self._wait)
            except TimeoutError:
                await self._say(self._texts.coder_permission_timeout)
                return {}
        finally:
            self._pending.pop(request_id, None)
            self._summaries.pop(request_id, None)
        return decision(allow)

    def summary(self, request_id: str) -> str | None:
        """How a waiting request reads, as the code wrote it; None when it no longer waits."""
        return self._summaries.get(str(request_id).strip())

    def answer(self, request_id: str, allow: bool) -> bool:
        """The user's answer to a waiting request; False when it no longer waits."""
        waiting = self._pending.get(str(request_id).strip())
        if waiting is None or waiting.done():
            return False
        waiting.set_result(allow)
        return True

    async def _notice(self, request: dict[str, Any]) -> None:
        """A prompt not asked here: a word that it waits in the Claude app, without its
        text, at most once a NOTICE_SECONDS."""
        now = self._clock()
        if now - self._last_notice < NOTICE_SECONDS:
            return
        self._last_notice = now
        tool = str(request.get("tool_name") or "?")[:40]
        await self._say(
            self._texts.coder_permission_in_app.format(
                where=_where(request, self._texts), tool=tool
            )
        )

    async def _say(self, text: str) -> None:
        if self.tell is None:
            return
        try:
            await self.tell(text)
        except Exception:
            log.warning("Could not tell about a coder permission prompt", exc_info=True)


def build_coder_permission_tools(approvals: Approvals | None, language: str = "en") -> list[Tool]:
    """The user's answer to a permission prompt of the coder. Allowing always waits for the
    user's go, shown as the code wrote it; refusing needs none."""
    if approvals is None:
        return []
    s = strings(language)
    gone = (
        "This request no longer waits (answered, or out of time): it is in the Claude app, "
        "where the user can still answer it."
    )

    def request_id(args: dict[str, Any]) -> str:
        return str(args.get("request") or "").strip()

    def vet(args: dict[str, Any], _: ToolContext) -> None:
        if approvals.summary(request_id(args)) is None:
            raise ToolError(gone)

    async def describe_allow(args: dict[str, Any]) -> str:
        return approvals.summary(request_id(args)) or s.coder_permission_summary.format(
            where="", tool="?", detail=""
        )

    async def allow(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        if not approvals.answer(request_id(args), True):
            raise ToolError(gone)
        return {"request": request_id(args), "status": "allowed"}

    async def refuse(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        if not approvals.answer(request_id(args), False):
            raise ToolError(gone)
        return {"request": request_id(args), "status": "refused"}

    schema = {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "The request's id, as it was given."}
        },
        "required": ["request"],
    }
    return [
        Tool(
            name="allow_coder_permission",
            description=(
                "Allow what Claude Code asked permission for in the coder container. It "
                "always waits for the user's own go, and is already waiting when the request "
                "was shown to them: their yes runs it through run_proposals."
            ),
            input_schema=schema,
            handler=allow,
            always_propose=True,
            describe=describe_allow,
            vet=vet,
        ),
        Tool(
            name="refuse_coder_permission",
            description=(
                "Refuse what Claude Code asked permission for in the coder container, when "
                "the user says no to it."
            ),
            input_schema=schema,
            handler=refuse,
        ),
    ]
