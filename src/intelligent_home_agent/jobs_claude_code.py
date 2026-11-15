"""Coding jobs run by Claude Code in the ``coder`` container (coder/app.py), on the user's
Claude subscription. The agent only talks HTTP to that container, on an internal network:
start a job, read its events, queue a message, interrupt. The coder holds the GitHub token
and the Claude login; the agent's own secrets never reach it, nor its secrets the agent."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from intelligent_home_agent.jobs import DONE, FAILED, IDLE, STOPPED, Event, SessionGone
from intelligent_home_agent.tools.base import ToolError

log = logging.getLogger(__name__)

POLL_SECONDS = 3.0
CLOSED = {DONE, FAILED, STOPPED}


class ClaudeCodeBackend:
    def __init__(
        self, http: httpx.AsyncClient, url: str, token: str, *, poll_seconds: float = POLL_SECONDS
    ) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._poll_seconds = poll_seconds

    def console_url(self, session: str) -> str:
        return f"{self._url}/jobs/{session}"

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._http.request(
                method, f"{self._url}{path}", json=body, headers=self._headers, timeout=60
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300]
            if exc.response.status_code == 404 and path != "/jobs":
                raise SessionGone(f"The coder does not know this job: {detail}") from exc
            raise ToolError(f"The coder refused: HTTP {exc.response.status_code} {detail}") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"The coder is unreachable: {exc}") from exc
        return resp.json()

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
        if rubric:
            brief = f"{brief}\n\nDone means every line of this holds:\n{rubric}"
        data = await self._call(
            "POST",
            "/jobs",
            {
                "id": job_id,
                "title": title,
                "brief": brief,
                "repo": repo,
                "branch": branch,
                "model": model,
            },
        )
        return str(data["session"])

    async def send(self, session: str, text: str) -> None:
        await self._call("POST", f"/jobs/{session}/messages", {"text": text})

    async def interrupt(self, session: str) -> None:
        await self._call("POST", f"/jobs/{session}/interrupt")

    async def follow(self, session: str) -> AsyncIterator[Event]:
        """Polls the coder for new events; the coder keeps them all, numbered, so a fresh
        connection replays from the first one."""
        after = 0
        while True:
            data = await self._call("GET", f"/jobs/{session}/events?after={after}")
            for item in data.get("events", []):
                after = max(after, int(item["n"]))
                kind = item.get("kind", "")
                if kind in ("said", "stopped", "ended"):
                    yield Event(
                        kind,
                        text=item.get("text", "") or "",
                        status=item.get("status") or IDLE,
                        at=item.get("at", "") or "",
                    )
            if data.get("status") in CLOSED:
                return
            await asyncio.sleep(self._poll_seconds)
