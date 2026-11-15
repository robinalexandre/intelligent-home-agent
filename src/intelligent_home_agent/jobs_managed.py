"""Coding jobs as Managed Agents sessions: Anthropic runs the agent loop and a sandbox with
git and a shell, billed to the API. The agent and its environment are created once by
scripts/jobs_setup.py from agents/coder.md and environments/coder.yaml. The repository is
mounted with the GitHub token, which goes through Anthropic's git proxy and never enters the
sandbox; the same token sits in a vault as GH_TOKEN for GitHub's API. Every session carries a
hard spend cap, and a repository job is an outcome: a grader checks the pull request against
the rubric and sends the coder back a few times.

This runner is Anthropic's own API (the ``anthropic`` SDK's beta sessions), not a model
call another provider could take: it is on only with LLM_PROVIDER=anthropic and its key."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import anthropic

from intelligent_home_agent.i18n import Strings, strings
from intelligent_home_agent.jobs import DENIED, FAILED, IDLE, Event, SessionGone
from intelligent_home_agent.tools.base import ToolError

log = logging.getLogger(__name__)

CONSOLE_URL = "https://platform.claude.com/workspaces/{workspace}/sessions/{session}"
MAX_SEEN_EVENTS = 2000


class ManagedAgentsBackend:
    def __init__(
        self,
        sessions: Any,
        *,
        agent_id: str,
        environment_id: str,
        github_token: str,
        vault_ids: list[str] | None = None,
        budget_usd: float = 10,
        max_iterations: int = 5,
        workspace: str = "default",
        texts: Strings | None = None,
    ) -> None:
        """``sessions`` is ``AsyncAnthropic().beta.sessions``."""
        self._sessions = sessions
        self._agent_id = agent_id
        self._environment_id = environment_id
        self._github_token = github_token
        self._vault_ids = vault_ids or []
        self._budget_cents = str(max(1, round(budget_usd * 100)))
        self._max_iterations = max_iterations
        self._workspace = workspace
        # The few words this runner adds to the coder's own (a refusal, the cap).
        self._texts = texts or strings("en")

    def console_url(self, session: str) -> str:
        return CONSOLE_URL.format(workspace=self._workspace, session=session)

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
        """``model`` is not used: a Managed Agents session runs the agent's own model, set
        in agents/coder.md."""
        events: list[dict[str, Any]] = [
            {"type": "user.message", "content": [{"type": "text", "text": brief}]}
        ]
        kwargs: dict[str, Any] = {}
        if repo:
            resource: dict[str, Any] = {
                "type": "github_repository",
                "url": f"https://github.com/{repo}",
                "authorization_token": self._github_token,
            }
            if branch:
                resource["checkout"] = {"type": "branch", "name": branch}
            kwargs["resources"] = [resource]
            events.append(
                {
                    "type": "user.define_outcome",
                    "description": f"Deliver the request above as a pull request on {repo}.",
                    "rubric": {"type": "text", "content": rubric},
                    "max_iterations": self._max_iterations,
                }
            )
        if self._vault_ids:
            kwargs["vault_ids"] = self._vault_ids
        try:
            session = await self._sessions.create(
                agent=self._agent_id,
                environment_id=self._environment_id,
                title=title,
                budget={
                    "type": "limit",
                    "max_list_cost": {"amount": self._budget_cents, "currency": "USD"},
                },
                metadata={"job": job_id},
                initial_events=events,
                **kwargs,
            )
        except anthropic.APIError as exc:
            raise ToolError(f"Could not start the job: {exc}") from exc
        return session.id

    async def send(self, session: str, text: str) -> None:
        await self._send(
            session, {"type": "user.message", "content": [{"type": "text", "text": text}]}
        )

    async def interrupt(self, session: str) -> None:
        await self._send(session, {"type": "user.interrupt"})

    async def _send(self, session: str, event: dict[str, Any]) -> None:
        try:
            await self._sessions.events.send(session, events=[event])
        except anthropic.APIError as exc:
            raise ToolError(f"The job did not take the message: {exc}") from exc

    async def follow(self, session: str) -> AsyncIterator[Event]:
        """The stream has no replay: the history is read first under the open stream, and
        events are deduplicated by id."""
        seen: deque[str] = deque(maxlen=MAX_SEEN_EVENTS)
        try:
            async with await self._sessions.events.stream(session) as stream:
                async for event in self._sessions.events.list(session, order="asc"):
                    async for out in self._take(session, event, seen):
                        yield out
                async for event in stream:
                    async for out in self._take(session, event, seen):
                        yield out
        except anthropic.NotFoundError as exc:
            raise SessionGone(f"No such session: {exc}") from exc

    async def _take(self, session: str, event: Any, seen: deque[str]) -> AsyncIterator[Event]:
        event_id = getattr(event, "id", "") or ""
        if event_id and event_id in seen:
            return
        if event_id:
            seen.append(event_id)
        kind = getattr(event, "type", "")
        at = _stamp(getattr(event, "processed_at", None))
        if kind == "agent.message":
            if text := _text_of(getattr(event, "content", None)):
                yield Event("said", text, at=at)
        elif kind in ("agent.tool_use", "agent.mcp_tool_use"):
            if getattr(event, "evaluated_permission", "") == "ask":
                await self._send(
                    session,
                    {
                        "type": "user.tool_confirmation",
                        "tool_use_id": event.id,
                        "result": "deny",
                        "deny_message": DENIED,
                    },
                )
                name = getattr(event, "name", "?")
                yield Event("said", self._texts.job_call_refused.format(tool=name), at=at)
        elif kind == "agent.custom_tool_use":
            await self._send(
                session,
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": event.id,
                    "content": [{"type": "text", "text": "This tool is not available."}],
                    "is_error": True,
                },
            )
        elif kind == "span.outcome_evaluation_end":
            result = getattr(event, "result", "")
            if result and result != "satisfied":
                explanation = getattr(event, "explanation", "") or ""
                yield Event(
                    "said",
                    self._texts.job_evaluation.format(result=result, explanation=explanation),
                    at=at,
                )
        elif kind == "session.error":
            error = getattr(event, "error", None)
            yield Event("said", f"⚠️ {getattr(error, 'message', None) or error or 'error'}", at=at)
        elif kind == "session.status_idle":
            reason = getattr(getattr(event, "stop_reason", None), "type", "") or ""
            if reason == "requires_action":
                return
            if reason == "budget_reached":
                yield Event("said", self._texts.job_budget_reached, at=at)
            yield Event("stopped", status=FAILED if reason == "retries_exhausted" else IDLE, at=at)
        elif kind == "session.status_terminated":
            yield Event("ended", at=at)


def _stamp(value: Any) -> str:
    """The SDK gives ``processed_at`` as a datetime; the runner compares stamps as strings."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return str(value or "")


def _text_of(content: Any) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p.strip() for p in parts if p.strip())
