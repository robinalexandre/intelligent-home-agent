"""Long-term memory: durable facts about the user, in a file they can read and edit.

One fact per line. The agent reads it at every request and adds or removes lines only when
the user asks, in a request that read nothing else: a fact comes back in every future
request, so an email or a web page must never be able to plant one.

The file lives in one of two places (MEMORY_STORE, docs/memory.md):

- ``LocalFile``: a file in DATA_DIR, the default. No account needed.
- ``GitHubFile``: a path in a GitHub repository, so the user reads and edits it from any
  computer and git keeps its history. The repository may encrypt the folder with
  git-crypt (config_crypt.py): the file is then read and written in that format.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.github import API_URL

CACHE_SECONDS = 300
MAX_FACT_CHARS = 300
MAX_MEMORY_CHARS = 6_000
# What must never be kept: keys and tokens by their shape, passwords by their name.
SECRET = re.compile(
    r"\b(ghp_|gho_|github_pat_|sk-|AKIA|xox[abp]-)[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY"
)
PASSWORD = re.compile(
    r"pass\s?word|passcode|passphrase|\bpwd\b|mot de passe|passwort|kennwort|contraseña"
    r"|wachtwoord|\bsenha\b",
    re.IGNORECASE,
)


def memory_header(assistant: str) -> str:
    return (
        f"# {assistant} — long-term memory\n\n"
        f"Durable facts {assistant} keeps about you, read at every request. One fact per "
        'line starting with "- "; edit or delete lines freely.\n\n'
    )


def folder_name(assistant: str) -> str:
    """The assistant's name as a folder of a repository: "Nova" keeps its files in nova/."""
    return re.sub(r"[^a-z0-9]+", "-", assistant.lower()).strip("-") or "assistant"


class TextFile(Protocol):
    """Where a file of the agent's lives. ``version`` is whatever ``read`` returned: a write
    made on an older version is refused (False), so an edit the user made meanwhile is
    never overwritten."""

    async def read(self) -> tuple[str | None, Any]: ...

    async def write(self, text: str, version: Any, message: str) -> bool: ...


class LocalFile:
    """A file on the agent's disk (DATA_DIR)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def read(self) -> tuple[str | None, Any]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise ToolError(f"{self._path.name} could not be read: {exc}") from exc
        return raw.decode("utf-8", "replace"), hashlib.sha256(raw).hexdigest()

    async def write(self, text: str, version: Any, message: str) -> bool:
        if (await self.read())[1] != version:  # edited by hand meanwhile
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self._path)
        except OSError as exc:
            raise ToolError(f"{self._path.name} could not be saved: {exc}") from exc
        return True


class GitHubFile:
    """A file of a GitHub repository, through the contents API. The token must be allowed
    to write the repository's contents."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        repo: str,
        path: str,
        crypt: ConfigCrypt | None = None,
        api_url: str = API_URL,
    ) -> None:
        self._http = http
        self._path = path.strip("/")
        self._url = f"{api_url.rstrip('/')}/repos/{repo}/contents/{self._path}"
        # The repository's encrypted folders (git-crypt): the file is kept in that format
        # there.
        self._crypt = crypt
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._encrypted = False

    async def read(self) -> tuple[str | None, Any]:
        try:
            resp = await self._http.get(self._url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ToolError(f"{self._path} unavailable: {exc}") from exc
        if resp.status_code == 404:
            return None, None
        if resp.is_error:
            raise ToolError(f"{self._path} unavailable: HTTP {resp.status_code}")
        data = resp.json()
        raw = base64.b64decode(data["content"])
        # A file found encrypted stays so when written back, whatever the perimeter says.
        self._encrypted = ConfigCrypt.is_encrypted(raw)
        if self._crypt is not None:
            try:
                raw = self._crypt.decrypt(raw)
            except CryptError as exc:
                raise ToolError(f"{self._path} unreadable: {exc}") from exc
        elif self._encrypted:
            raise ToolError(f"{self._path} is encrypted (git-crypt) and the agent has no key.")
        return raw.decode("utf-8", "replace"), data["sha"]

    async def write(self, text: str, version: Any, message: str) -> bool:
        raw = text.encode()
        if self._crypt is not None:
            try:
                raw = (
                    self._crypt.encrypt(raw)
                    if self._encrypted
                    else self._crypt.encode(self._path, raw)
                )
            except CryptError as exc:
                raise ToolError(f"{self._path} could not be encrypted: {exc}") from exc
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(raw).decode()}
        if version:
            body["sha"] = version
        try:
            resp = await self._http.put(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ToolError(f"{self._path} unavailable: {exc}") from exc
        if resp.status_code in (409, 422):  # changed meanwhile (the user edited it)
            return False
        if resp.is_error:
            raise ToolError(f"{self._path} could not be saved: HTTP {resp.status_code}")
        return True


class Memory:
    def __init__(self, file: TextFile, timezone: str, assistant: str = "Assistant") -> None:
        self._file = file
        self._timezone = timezone
        self._assistant = assistant
        self._header = memory_header(assistant)
        self._lock = asyncio.Lock()
        self._cached: tuple[str, float] | None = None

    async def read(self) -> str:
        """The file's text; "" when it does not exist yet."""
        return (await self._file.read())[0] or ""

    async def update(self, change: Callable[[str], tuple[str, Any]], message: str) -> Any:
        """Apply ``change`` (text -> new text, result) to the file as it is now, again when
        it was edited meanwhile. ``message``: the commit message, where there is one."""
        async with self._lock:
            for _ in range(3):
                text, version = await self._file.read()
                new_text, result = change(text or "")
                if new_text == (text or ""):
                    return result
                if await self._file.write(new_text, version, message):
                    self._cached = None
                    return result
            raise ToolError("The file keeps changing: try again in a moment.")

    async def facts(self) -> list[str]:
        if self._cached and time.monotonic() - self._cached[1] < CACHE_SECONDS:
            text = self._cached[0]
        else:
            text = await self.read()
            self._cached = (text, time.monotonic())
        return [line[2:].strip() for line in text.splitlines() if line.startswith("- ")]

    async def prompt_text(self) -> str:
        """The facts for the system prompt, newest kept when there are too many."""
        try:
            facts = await self.facts()
        except ToolError:
            return ""
        kept: list[str] = []
        size = 0
        for fact in reversed(facts):
            size += len(fact) + 3
            if size > MAX_MEMORY_CHARS:
                break
            kept.append(f"- {fact}")
        return "\n".join(reversed(kept))

    async def remember(self, fact: str) -> str:
        fact = " ".join(fact.split())[:MAX_FACT_CHARS]
        if not fact:
            raise ToolError("fact is required.")
        if SECRET.search(fact) or PASSWORD.search(fact):
            raise ToolError("Never keep passwords, keys or tokens in memory.")
        today = datetime.now(ZoneInfo(self._timezone)).strftime("%Y-%m-%d")
        line = f"- {fact} ({today})"

        def add(text: str) -> tuple[str, str]:
            if any(fact in existing for existing in text.splitlines()):
                return text, "already known"
            return (text or self._header).rstrip("\n") + "\n" + line + "\n", "remembered"

        return await self.update(add, f"chore(memory): {self._assistant} remembers a fact")

    async def forget(self, words: str) -> int:
        needle = words.strip().lower()
        if len(needle) < 3:
            raise ToolError("Say which fact to forget, in a few words.")

        def remove(text: str) -> tuple[str, int]:
            lines = text.splitlines(keepends=True)
            kept = [ln for ln in lines if not (ln.startswith("- ") and needle in ln.lower())]
            return "".join(kept), len(lines) - len(kept)

        return await self.update(remove, f"chore(memory): {self._assistant} forgets a fact")


def build_memory_tools(memory: Memory | None, language: str = "en") -> list[Tool]:
    if memory is None:
        return []
    s = strings(language)

    async def remember(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return {"result": await memory.remember(str(args.get("fact") or ""))}

    async def forget(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        removed = await memory.forget(str(args.get("words") or ""))
        return {"removed": removed}

    async def describe_remember(args: dict[str, Any]) -> str:
        return s.remember_summary.format(fact=args.get("fact") or "")

    async def describe_forget(args: dict[str, Any]) -> str:
        return s.forget_summary.format(words=args.get("words") or "")

    return [
        Tool(
            name="remember",
            description=(
                "Keep a durable fact about the user for every future conversation: people "
                '("my plumber is Sam Lee, sam@example.com"), preferences, accounts, '
                "habits. One short sentence. Only when the user asks or tells you such a "
                "fact."
            ),
            input_schema={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            handler=remember,
            needs_clean_turn=True,
            # After a read or in a quoted reply, the user's go keeps it: the list under the
            # reply shows the very sentence that would go into every future prompt.
            proposable=True,
            describe=describe_remember,
        ),
        Tool(
            name="forget",
            description="Remove the remembered facts containing these words.",
            input_schema={
                "type": "object",
                "properties": {"words": {"type": "string"}},
                "required": ["words"],
            },
            handler=forget,
            needs_clean_turn=True,
            describe=describe_forget,
        ),
    ]
