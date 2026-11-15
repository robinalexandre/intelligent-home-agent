"""The user's servers at a hosting provider, through the provider's public API and a token.

One set of tools whatever the provider (``list_servers``, ``get_server_metrics``,
``restart_server``...): a provider is a class with one method per capability, and a tool
exists only when the provider has its method. Hostinger (VPS, with its Docker projects) is
the first; Hetzner Cloud the second, written from its documentation (docs/hosting.md says
what was run and what was not).

A token often opens the whole account (domains, billing...), so only an allowlist is
exposed: reading the servers, their metrics, backups, firewalls and, where the provider
has them, malware scans and Docker projects, and a few actions (restart a project or the
server, take a snapshot, deploy a compose file). Every result taints the request, as logs
and configs hold text others wrote, and every action waits for the user's go: run directly
only on their own clear request, otherwise parked; a deployment always waits. A server
often hosts several things: each action names the server and the project it touches.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_RESULT_CHARS = 12_000
RETRY_SECONDS = 3.0
_PROJECT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Compose settings that give a container power over the host, flagged to the user. The
# second item is a key of Strings.hosting_risky.
RISKY_COMPOSE = [
    (r"privileged:\s*true", "privileged"),
    (r"network_mode:\s*[\"']?host", "host_network"),
    (r"pid:\s*[\"']?host", "host_pid"),
    (r"cap_add:", "cap_add"),
    (r"docker\.sock", "docker_socket"),
    (r"^\s*-\s*[\"']?/[^:\n]*:", "host_folder"),
]


class HostingApi:
    """A provider's HTTP API behind a bearer token. A subclass adds one method per
    capability it has, named as in READS and WRITES below."""

    name = ""
    url = ""
    # Where the user makes a token, for the error that says theirs was refused.
    renew_hint = ""
    # True when a new snapshot replaces the previous one; False when each one is kept
    # (and billed) until deleted. The user is told which before giving their go.
    snapshot_replaces = False

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        url: str | None = None,
        retry_seconds: float = RETRY_SECONDS,
    ) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._url = (url or self.url).rstrip("/")
        self._retry_seconds = retry_seconds

    async def call(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> Any:
        # A read is tried twice: such an API may fail for a moment on its side. An action
        # is sent once, as it may have run despite the error.
        attempts = 2 if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._http.request(
                    method,
                    f"{self._url}{path}",
                    params=params,
                    json=body,
                    headers=self._headers,
                    timeout=60,
                )
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    await asyncio.sleep(self._retry_seconds)
                    continue
                raise ToolError(f"{self.name} unavailable: {exc}") from exc
            if resp.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(self._retry_seconds)
                continue
            break
        if resp.status_code == 401:
            raise ToolError(f"{self.name} refused the token: {self.renew_hint}")
        if resp.status_code >= 500 and attempts > 1:
            raise ToolError(
                f"{self.name}'s API is failing on its side (HTTP {resp.status_code}), already"
                " retried: don't repeat this call in this request; say in one short line that"
                " this check could not be done."
            )
        if resp.status_code >= 500:
            raise ToolError(
                f"{self.name}'s API failed on its side (HTTP {resp.status_code}): the action"
                " may have run anyway; look at its state before trying again."
            )
        if resp.is_error:
            raise ToolError(f"{self.name} error (HTTP {resp.status_code}): {resp.text[:300]}")
        return resp.json() if resp.content else {}

    async def server_name(self, server: str) -> str:
        raise NotImplementedError


class Hostinger(HostingApi):
    """Hostinger's VPS API (https://developers.hostinger.com). The token is the account's:
    only the paths below are ever called."""

    name = "Hostinger"
    url = "https://developers.hostinger.com"
    renew_hint = "create a new one in hPanel > API."
    snapshot_replaces = True
    _vm = "/api/vps/v1/virtual-machines"

    def _docker(self, server: str, project: str, what: str = "") -> str:
        return f"{self._vm}/{server}/docker/{quote(project)}{what}"

    async def server_name(self, server: str) -> str:
        data = await self.call("GET", f"{self._vm}/{server}")
        return str(data.get("hostname") or data.get("id") or server)

    async def list_servers(self) -> Any:
        return await self.call("GET", self._vm)

    async def get_server(self, server: str) -> Any:
        return await self.call("GET", f"{self._vm}/{server}")

    async def get_metrics(self, server: str, start: datetime, end: datetime) -> Any:
        window = {
            "date_from": start.isoformat(timespec="seconds"),
            "date_to": end.isoformat(timespec="seconds"),
        }
        return await self.call("GET", f"{self._vm}/{server}/metrics", window)

    async def list_actions(self, server: str) -> Any:
        return await self.call("GET", f"{self._vm}/{server}/actions")

    async def list_backups(self, server: str) -> Any:
        return await self.call("GET", f"{self._vm}/{server}/backups")

    async def get_malware_scan(self, server: str) -> Any:
        return await self.call("GET", f"{self._vm}/{server}/monarx")

    async def list_firewalls(self) -> Any:
        return await self.call("GET", "/api/vps/v1/firewall")

    async def list_projects(self, server: str) -> Any:
        return await self.call("GET", f"{self._vm}/{server}/docker")

    async def get_project(self, server: str, project: str) -> Any:
        return await self.call("GET", self._docker(server, project))

    async def list_project_containers(self, server: str, project: str) -> Any:
        return await self.call("GET", self._docker(server, project, "/containers"))

    async def get_project_logs(self, server: str, project: str) -> Any:
        return await self.call("GET", self._docker(server, project, "/logs"))

    async def restart_project(self, server: str, project: str) -> Any:
        return await self.call("POST", self._docker(server, project, "/restart"))

    async def start_project(self, server: str, project: str) -> Any:
        return await self.call("POST", self._docker(server, project, "/start"))

    async def stop_project(self, server: str, project: str) -> Any:
        return await self.call("POST", self._docker(server, project, "/stop"))

    async def update_project(self, server: str, project: str) -> Any:
        return await self.call("POST", self._docker(server, project, "/update"))

    async def restart_server(self, server: str) -> Any:
        return await self.call("POST", f"{self._vm}/{server}/restart")

    async def take_snapshot(self, server: str) -> Any:
        return await self.call("POST", f"{self._vm}/{server}/snapshot")

    async def deploy_project(self, server: str, project: str, content: str) -> Any:
        body = {"project_name": project, "content": content}
        return await self.call("POST", f"{self._vm}/{server}/docker", body=body)


class Hetzner(HostingApi):
    """Hetzner Cloud (https://docs.hetzner.cloud). Written from its documentation and never
    run against the real API. A token belongs to one project and is read-only or
    read-write: with a read-only one the actions are refused by Hetzner itself. No Docker
    projects and no malware scan there, so those tools are absent."""

    name = "Hetzner"
    url = "https://api.hetzner.cloud/v1"
    renew_hint = "create a new one in the Cloud Console, in the project: Security > API tokens."

    async def server_name(self, server: str) -> str:
        data = (await self.get_server(server)).get("server") or {}
        return str(data.get("name") or data.get("id") or server)

    async def list_servers(self) -> Any:
        return await self.call("GET", "/servers", {"per_page": 50})

    async def get_server(self, server: str) -> Any:
        return await self.call("GET", f"/servers/{server}")

    async def get_metrics(self, server: str, start: datetime, end: datetime) -> Any:
        # About sixty points whatever the window. Hetzner has no memory metric.
        params = {
            "type": "cpu,disk,network",
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "step": max(60, int((end - start).total_seconds() // 60)),
        }
        return await self.call("GET", f"/servers/{server}/metrics", params)

    async def list_actions(self, server: str) -> Any:
        params = {"sort": "started:desc", "per_page": 25}
        return await self.call("GET", f"/servers/{server}/actions", params)

    async def list_backups(self, server: str) -> Any:
        backups = await self.call("GET", "/images", {"type": "backup", "bound_to": server})
        snapshots = await self.call("GET", "/images", {"type": "snapshot", "per_page": 50})
        of_server = [
            image
            for image in snapshots.get("images") or []
            if str((image.get("created_from") or {}).get("id")) == server
        ]
        return {"backups": backups.get("images") or [], "snapshots": of_server}

    async def list_firewalls(self) -> Any:
        return await self.call("GET", "/firewalls", {"per_page": 50})

    async def restart_server(self, server: str) -> Any:
        # A soft reboot (the system is asked to restart), not a power reset.
        return await self.call("POST", f"/servers/{server}/actions/reboot")

    async def take_snapshot(self, server: str) -> Any:
        body = {"type": "snapshot", "description": "Taken by the home agent"}
        return await self.call("POST", f"/servers/{server}/actions/create_image", body=body)


PROVIDERS: dict[str, type[HostingApi]] = {"hostinger": Hostinger, "hetzner": Hetzner}


def build_hosting_provider(
    name: str | None, token: str | None, http: httpx.AsyncClient
) -> HostingApi | None:
    if not name or not token:
        return None
    return PROVIDERS[name](http, token)


# tool, the provider's method, what it does, whether it is about one server, one project.
READS = [
    ("list_servers", "list_servers", "The user's servers: id, name, state, IP, plan.", 0, 0),
    ("get_server", "get_server", "One server in detail: state, resources, OS, IPs.", 1, 0),
    ("get_server_metrics", "get_metrics",
     "CPU, disk, network (and what else the provider measures) of a server over the last "
     "hours (default 24).", 1, 0),
    ("list_server_actions", "list_actions",
     "Recent actions on a server (restarts, backups...) and their status.", 1, 0),
    ("list_server_backups", "list_backups",
     "The backups and snapshots of a server, with their dates.", 1, 0),
    ("get_server_malware_scan", "get_malware_scan",
     "The malware scanner's latest results on a server.", 1, 0),
    ("list_server_firewalls", "list_firewalls", "The firewalls and their rules.", 0, 0),
    ("list_server_projects", "list_projects",
     "The Docker projects of a server and their state.", 1, 0),
    ("get_server_project", "get_project",
     "One Docker project: its compose file and settings.", 1, 1),
    ("list_server_project_containers", "list_project_containers",
     "The containers of a Docker project and their state.", 1, 1),
    ("get_server_project_logs", "get_project_logs",
     "The latest logs of a Docker project's containers.", 1, 1),
]  # fmt: skip
# tool, the provider's method, what it does, the key of Strings.hosting_actions, project.
WRITES = [
    ("restart_server_project", "restart_project", "Restart a Docker project's containers.",
     "restart_project", 1),
    ("start_server_project", "start_project", "Start a stopped Docker project.",
     "start_project", 1),
    ("stop_server_project", "stop_project", "Stop a Docker project.", "stop_project", 1),
    ("update_server_project", "update_project",
     "Pull a Docker project's images again and recreate its containers.", "update_project", 1),
    ("restart_server", "restart_server",
     "Restart a whole server: everything on it goes down a moment.", "restart_server", 0),
    ("take_server_snapshot", "take_snapshot", "", "snapshot", 0),
]  # fmt: skip


def _server(args: dict[str, Any]) -> str:
    value = str(args.get("server_id") or "").strip()
    if not value.isdigit():
        raise ToolError("server_id is the server's number from list_servers.")
    return value


def _project(args: dict[str, Any]) -> str:
    value = str(args.get("project") or "").strip()
    if not _PROJECT.match(value):
        raise ToolError("project is a Docker project name from list_server_projects.")
    return value


def _short(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text[:MAX_RESULT_CHARS]


def risky_settings(compose: str) -> list[str]:
    """The keys of what a compose file asks for that gives a container power over the host."""
    return [key for pattern, key in RISKY_COMPOSE if re.search(pattern, compose, re.M)]


def build_hosting_tools(api: HostingApi | None, language: str = "en") -> list[Tool]:
    if api is None:
        return []
    words = strings(language)
    who = api.name

    def schema(server: int, project: int, more: dict[str, Any] | None = None) -> dict[str, Any]:
        props: dict[str, Any] = {}
        if server:
            props["server_id"] = {"type": "string", "description": "From list_servers."}
        if project:
            props["project"] = {"type": "string", "description": "From list_server_projects."}
        props |= more or {}
        return {"type": "object", "properties": props, "required": list(props)}

    def placed(method: Any, server: int, project: int) -> Any:
        async def handler(args: dict[str, Any], _: ToolContext) -> str:
            where = ([_server(args)] if server else []) + ([_project(args)] if project else [])
            return _short(await method(*where))

        return handler

    async def metrics(args: dict[str, Any], _: ToolContext) -> str:
        hours = min(max(int(args.get("hours") or 24), 1), 24 * 7)
        now = datetime.now(UTC)
        return _short(await api.get_metrics(_server(args), now - timedelta(hours=hours), now))

    def describing(what: str) -> Any:
        async def describe(args: dict[str, Any]) -> str:
            server = await api.server_name(_server(args))
            if args.get("project"):
                return words.hosting_project_summary.format(
                    provider=who, what=what, project=args["project"], server=server
                )
            return words.hosting_server_summary.format(provider=who, what=what, server=server)

        return describe

    async def describe_deploy(args: dict[str, Any]) -> str:
        head = await describing(words.hosting_actions["deploy_project"])(args)
        content = str(args.get("content") or "")
        risky = [words.hosting_risky[key] for key in risky_settings(content)]
        warning = words.hosting_deploy_warning.format(risks=", ".join(risky)) if risky else ""
        return words.hosting_deploy_summary.format(
            head=head, warning=warning, compose=content.replace("\n", " ⏎ ")
        )

    async def deploy(args: dict[str, Any], _: ToolContext) -> str:
        content = str(args.get("content") or "")
        if re.match(r"\s*https?://", content):
            # A link can change after the user approved it: only the YAML itself.
            raise ToolError("Give the compose file itself, not a link to it.")
        return _short(await api.deploy_project(_server(args), _project(args), content))

    tools: list[Tool] = []
    for name, method, text, server, project in READS:
        if not hasattr(api, method):
            continue
        hours = {"hours": {"type": "integer"}} if method == "get_metrics" else None
        input_schema = schema(server, project, hours)
        if hours:
            input_schema["required"] = ["server_id"]
        tools.append(
            Tool(
                name=name,
                description=f"{who}, read-only. {text}",
                input_schema=input_schema,
                handler=(
                    metrics
                    if method == "get_metrics"
                    else placed(getattr(api, method), server, project)
                ),
                taints=True,
            )
        )
    snapshot = "snapshot_replacing" if api.snapshot_replaces else "snapshot_kept"
    snapshot_text = (
        "Take a snapshot of a server (it replaces the previous one), e.g. before a change."
        if api.snapshot_replaces
        else "Take a snapshot of a server, e.g. before a change. It is kept, and billed by "
        "the provider, until the user deletes it."
    )
    for name, method, text, what, project in WRITES:
        if not hasattr(api, method):
            continue
        tools.append(
            Tool(
                name=name,
                description=(
                    f"{who}, action. {text or snapshot_text} Only on the user's clear "
                    "request; otherwise it waits for their go."
                ),
                input_schema=schema(1, project),
                handler=placed(getattr(api, method), 1, project),
                needs_clean_turn=True,
                needs_user_request=True,
                proposable=True,
                describe=describing(
                    words.hosting_actions[snapshot if method == "take_snapshot" else what]
                ),
            )
        )
    if hasattr(api, "deploy_project"):
        tools.append(
            Tool(
                name="deploy_server_project",
                description=(
                    f"{who}, action. Deploy a Docker project from a compose file (the YAML "
                    "itself, never a link), creating it or replacing its configuration. "
                    "Always waits for the user's go; read the current one first "
                    "(get_server_project)."
                ),
                input_schema=schema(
                    1, 1, {"content": {"type": "string", "description": "The compose YAML"}}
                ),
                handler=deploy,
                needs_clean_turn=True,
                needs_user_request=True,
                proposable=True,
                always_propose=True,
                describe=describe_deploy,
                multiline_args=frozenset({"content"}),
            )
        )
    return tools
