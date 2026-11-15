"""A small MCP client over streamable HTTP, for the remote MCP servers the user connects
(tools/connectors.py), so each of their tools goes through the tool registry and its rules.

Three ways to log in, by what the server offers: a fixed token, OAuth client credentials,
or an OAuth refresh token obtained once with scripts/mcp_login.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from intelligent_home_agent.tools.base import ToolError

PROTOCOL_VERSION = "2025-06-18"


class TokenSource(Protocol):
    async def access_token(self, *, force: bool = False) -> str: ...


class NoAuth:
    async def access_token(self, *, force: bool = False) -> str:
        return ""


class StaticToken:
    def __init__(self, token: str) -> None:
        self._token = token

    async def access_token(self, *, force: bool = False) -> str:
        return self._token


class OAuthToken:
    """Access tokens from OAuth client credentials, or from a refresh token.

    A server may rotate the refresh token at each use: the new one is saved in ``store``
    (on the data volume), and read from there at the next start.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        label: str,
        token_url: str,
        client_id: str,
        *,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        resource: str | None = None,
        scope: str | None = None,
        store: Path | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._http, self._label, self._token_url = http, label, token_url
        self._client_id, self._client_secret = client_id, client_secret
        self._refresh_token, self._first_refresh_token = refresh_token, refresh_token
        self._resource, self._scope = resource, scope
        self._store, self._clock = store, clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        if refresh_token and store is not None and store.exists():
            saved = json.loads(store.read_text())
            # A new login in the settings wins over what an older one left on disk.
            if saved.get("client_id") == client_id and saved.get("issued_from") == refresh_token:
                self._refresh_token = saved.get("refresh_token") or refresh_token

    async def access_token(self, *, force: bool = False) -> str:
        if not force and self._access_token and self._clock() < self._expires_at - 60:
            return self._access_token
        data = {"client_id": self._client_id}
        if self._refresh_token:
            data |= {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        else:
            data["grant_type"] = "client_credentials"
        for key, value in (
            ("client_secret", self._client_secret),
            ("resource", self._resource),
            ("scope", self._scope),
        ):
            if value:
                data[key] = value
        try:
            resp = await self._http.post(self._token_url, data=data)
        except httpx.HTTPError as exc:
            raise ToolError(f"{self._label} unavailable: {type(exc).__name__}") from exc
        if resp.is_error:
            raise ToolError(
                f"{self._label} refused the login: check its credentials in MCP_SERVERS"
                + (", or run scripts/mcp_login.py again." if self._refresh_token else ".")
            )
        answer = resp.json()
        self._access_token = str(answer["access_token"])
        self._expires_at = self._clock() + float(answer.get("expires_in", 300))
        rotated = answer.get("refresh_token")
        if rotated and rotated != self._refresh_token:
            self._refresh_token = rotated
            self._save()
        return self._access_token

    def _save(self) -> None:
        if self._store is None:
            return
        self._store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "client_id": self._client_id,
                    "issued_from": self._first_refresh_token,
                    "refresh_token": self._refresh_token,
                }
            )
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self._store)


def _message(resp: httpx.Response, request_id: int, label: str) -> dict[str, Any]:
    """A JSON-RPC reply, sent either as plain JSON or as server-sent events."""
    if "text/event-stream" not in resp.headers.get("content-type", ""):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:].strip())
            if message.get("id") == request_id:
                return message
    raise ToolError(f"{label} sent no answer.")


class MCPClient:
    def __init__(self, http: httpx.AsyncClient, auth: TokenSource, url: str, label: str) -> None:
        self._http = http
        self._auth = auth
        self._url = url
        self.label = label
        self._session_id: str | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if token := await self._auth.access_token():
            headers["Authorization"] = f"Bearer {token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            return await self._http.post(self._url, json=payload, headers=headers, timeout=60)
        except httpx.HTTPError as exc:
            raise ToolError(f"{self.label} unavailable: {type(exc).__name__}") from exc

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _open_session(self) -> None:
        self._session_id = None
        request_id = self._new_id()
        resp = await self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "intelligent-home-agent", "version": "1"},
                },
            }
        )
        if resp.is_error:
            raise ToolError(f"{self.label} refused the connection: HTTP {resp.status_code}")
        _message(resp, request_id, self.label)
        self._session_id = resp.headers.get("mcp-session-id")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._session_id is None:
                await self._open_session()
            for attempt in range(2):
                request_id = self._new_id()
                resp = await self._post(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                if resp.status_code in (401, 404) and attempt == 0:
                    # Expired token or session: log in and open a new session once.
                    if resp.status_code == 401:
                        await self._auth.access_token(force=True)
                    await self._open_session()
                    continue
                if resp.is_error:
                    raise ToolError(f"{self.label} error: HTTP {resp.status_code}")
                message = _message(resp, request_id, self.label)
                if "error" in message:
                    raise ToolError(f"{self.label} error: {message['error'].get('message')}")
                return message["result"]
        raise ToolError(f"{self.label} keeps refusing the session.")

    async def list_tools(self) -> list[dict[str, Any]]:
        return (await self._request("tools/list", {})).get("tools", [])

    def _tool_error(self, text: str) -> ToolError:
        return ToolError(f"{self.label}: {text[:500]}")

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """A tool's whole answer as text; an error the server reports is raised."""
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            block.get("text", "") for block in result.get("content", []) if block["type"] == "text"
        )
        if result.get("isError"):
            raise self._tool_error(text)
        return text

    async def call_json(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """A tool's answer as data, for the code's own use (never cut short)."""
        text = await self.call(name, arguments)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ToolError(f"{self.label} sent an unexpected answer to {name}.") from exc
        return data if isinstance(data, dict) else {"items": data}
