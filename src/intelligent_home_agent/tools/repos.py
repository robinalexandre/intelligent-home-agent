"""Read-only access to the user's GitHub repositories, their notes repository first.

What the token can see is what the agent can read: a fine-grained token limited to a
few repositories limits it to those. Nothing is ever written here."""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

import httpx

from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.github import API_URL

# Longer files come in parts (tools/long_results.py).
MAX_FILE_CHARS = 150_000
MAX_ENTRIES = 200
MAX_REPOS = 50
MAX_SEARCH_RESULTS = 10
_REPO = re.compile(r"[\w.-]+/[\w.-]+")


class Repositories:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        default_repo: str | None = None,
        api_url: str = API_URL,
        crypt: ConfigCrypt | None = None,
    ) -> None:
        self._http = http
        self._token = token
        self.default_repo = default_repo
        self._api_url = api_url.rstrip("/")
        # The notes repository's encrypted folders (git-crypt), read with the user's key.
        self._crypt = crypt

    def _text(self, data: bytes) -> str:
        if ConfigCrypt.is_encrypted(data):
            if self._crypt is None:
                raise ToolError("This file is encrypted (git-crypt) and the agent has no key.")
            try:
                data = self._crypt.decrypt(data)
            except CryptError as exc:
                raise ToolError(f"Encrypted file the agent cannot read: {exc}") from exc
        return data.decode("utf-8", errors="replace")

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, accept: str | None = None
    ) -> Any:
        try:
            resp = await self._http.get(
                f"{self._api_url}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": accept or "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if resp.status_code == 404:
                raise ToolError("Not found: check the repository name and the path.")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"GitHub unavailable: {exc}") from exc
        return resp.json()

    async def owned(self) -> list[dict[str, Any]]:
        repos = await self._get(
            "/user/repos", {"sort": "pushed", "per_page": MAX_REPOS, "affiliation": "owner"}
        )
        return [
            {
                "repo": r["full_name"],
                "description": r.get("description"),
                "private": r.get("private", False),
                "last_push": (r.get("pushed_at") or "")[:10],
            }
            for r in repos
        ]

    async def contents(self, repo: str, path: str) -> dict[str, Any]:
        data = await self._get(f"/repos/{repo}/contents/{quote(path.strip('/'))}")
        if isinstance(data, list):
            entries = [
                {"name": e["name"], "type": e["type"]}
                for e in sorted(data, key=lambda e: (e["type"] != "dir", e["name"]))
            ]
            return {"repo": repo, "folder": path or "/", "entries": entries[:MAX_ENTRIES]}
        if data.get("type") != "file" or "content" not in data:
            raise ToolError(f"{path} is not a file or a folder that can be read.")
        text = self._text(base64.b64decode(data["content"]))
        return {
            "repo": repo,
            "file": data["path"],
            "content": text[:MAX_FILE_CHARS],
            "truncated": len(text) > MAX_FILE_CHARS,
        }

    async def tree(self, repo: str) -> list[str]:
        """Every file path of the default branch."""
        data = await self._get(f"/repos/{repo}/git/trees/HEAD", {"recursive": "1"})
        return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]

    async def raw(self, repo: str, path: str, max_bytes: int) -> str:
        """A whole file, past the size the contents listing gives inline."""
        chunks, size = [], 0
        try:
            async with self._http.stream(
                "GET",
                f"{self._api_url}/repos/{repo}/contents/{quote(path.strip('/'))}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=60,
            ) as resp:
                resp.raise_for_status()
                if int(resp.headers.get("content-length") or 0) > max_bytes:
                    raise ToolError(f"{path} is too big to read.")
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:  # stops reading there, whatever was announced
                        raise ToolError(f"{path} is too big to read.")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ToolError(f"GitHub unavailable: {exc}") from exc
        return self._text(b"".join(chunks))

    async def search(self, query: str, repo: str) -> list[dict[str, Any]]:
        data = await self._get(
            "/search/code",
            {"q": f"{query} repo:{repo}", "per_page": MAX_SEARCH_RESULTS},
            accept="application/vnd.github.text-match+json",
        )
        return [
            {
                "path": item["path"],
                "matches": [m["fragment"] for m in item.get("text_matches", [])][:3],
            }
            for item in data.get("items", [])
        ]


def build_repository_tools(repos: Repositories | None) -> list[Tool]:
    if repos is None:
        return []

    def repo_of(args: dict[str, Any]) -> str:
        repo = str(args.get("repo") or repos.default_repo or "").strip()
        if not _REPO.fullmatch(repo):
            raise ToolError('repo must look like "owner/name", from list_repositories.')
        return repo

    async def list_repositories(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"notes_repo": repos.default_repo, "repositories": await repos.owned()}

    async def browse_repository(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return await repos.contents(repo_of(args), args.get("path") or "")

    async def search_repository(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required.")
        repo = repo_of(args)
        return {"repo": repo, "results": await repos.search(query, repo)}

    default = (
        f"defaults to the user's notes repository {repos.default_repo}"
        if repos.default_repo
        else "from list_repositories"
    )
    repo_property = {"type": "string", "description": f'"owner/name"; {default}.'}
    notes = (
        f", by default their notes repository ({repos.default_repo}): reminders, handoff "
        "notes, notes on their projects and setup"
        if repos.default_repo
        else ""
    )
    return [
        Tool(
            name="list_repositories",
            description="The user's own GitHub repositories, most recently pushed first.",
            input_schema={"type": "object", "properties": {}},
            handler=list_repositories,
            taints=True,
        ),
        Tool(
            name="browse_repository",
            description=(
                "List a folder or read a text file in one of the user's GitHub repositories"
                f"{notes}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": repo_property,
                    "path": {
                        "type": "string",
                        "description": "Folder or file path; empty for the repository root.",
                    },
                },
            },
            handler=browse_repository,
            taints=True,
        ),
        Tool(
            name="search_repository",
            description=(
                "Search the files of one of the user's GitHub repositories for words. "
                "Returns paths and matching lines."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}, "repo": repo_property},
                "required": ["query"] if repos.default_repo else ["query", "repo"],
            },
            handler=search_repository,
            taints=True,
        ),
    ]
