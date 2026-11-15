"""Notes the agent leaves in a GitHub repository for the user's later coding sessions.

A reminder (``reminders/``) or a handoff note (``handoffs/``) is one Markdown file with a
front matter, committed through the GitHub API. The formats are the ones of the reminders
and handoff skills of claude-code-kit (https://github.com/robinalexandre/claude-code-kit),
whose sync shows them at the start of a coding session on every computer; any other tool,
or a person, can read the same files. Each note is a new file, so it can't conflict with
what the user changed meanwhile.

The same file format, in a folder of the agent's disk (LocalFolder) or of that repository
(RepoFolder), holds the tasks the agent runs by itself (tasks.py).
"""

from __future__ import annotations

import base64
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.assistant_reminders import ReminderRepo, parse
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, describe_whole
from intelligent_home_agent.tools.github import API_URL

AUTHOR = "assistant"
MAX_ATTEMPTS = 5
PAST = "past"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
_ID = re.compile(r"^\w[\w.-]*$")
_SECRET = re.compile(
    r"\b(ghp_|gho_|github_pat_|sk-ant-|sk-proj-|AKIA|xox[abp]-)[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY"
)


def slugify(text: str, max_length: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "note"


def one_line(value: str) -> str:
    return " ".join(value.split())


def refuse_secrets(text: str) -> None:
    if _SECRET.search(text):
        raise ToolError("This looks like it contains a key or token: leave it out.")


def note_id(value: Any, source: str) -> str:
    """A note's id as a tool gave it: a file name without .md, never a path."""
    value = str(value or "").strip().removesuffix(".md")
    if not value:
        raise ToolError("id is required.")
    if not _ID.match(value) or ".." in value:
        raise ToolError(f"id is an id from {source}, e.g. 2026-10-24-call-the-plumber.")
    return value


class NotesRepo:
    """A GitHub repository the agent adds notes to, through the contents API."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: str,
        repo: str,
        api_url: str = API_URL,
        crypt: ConfigCrypt | None = None,
        author: str = AUTHOR,
    ) -> None:
        self._http = http
        self._token = token
        # Who the commits and the handoff notes say they are from: the assistant's name.
        self.author = author
        self._base = f"{api_url.rstrip('/')}/repos/{repo}/contents"
        # The repository's encrypted folders (git-crypt): written and read in that format.
        self._crypt = crypt

    def _encode(self, path: str, text: str, encrypted: bool = False) -> str:
        """``encrypted``: the file was found encrypted, so it stays so whatever the perimeter."""
        data = text.encode()
        if self._crypt is not None:
            data = self._crypt.encrypt(data) if encrypted else self._crypt.encode(path, data)
        return base64.b64encode(data).decode()

    def _decode(self, content: str) -> tuple[str, bool]:
        data = base64.b64decode(content)
        encrypted = ConfigCrypt.is_encrypted(data)
        if self._crypt is not None:
            try:
                data = self._crypt.decrypt(data)
            except CryptError as exc:
                raise ToolError(f"Encrypted file the agent cannot read: {exc}") from exc
        elif encrypted:
            raise ToolError("This file is encrypted (git-crypt) and the agent has no key.")
        return data.decode("utf-8", "replace"), encrypted

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            return await self._http.request(
                method,
                f"{self._base}/{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"GitHub unavailable: {exc}") from exc

    async def _add(self, stem: str, text: str, encrypted: bool = False) -> str:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            path = f"{stem}.md" if attempt == 1 else f"{stem}-{attempt}.md"
            message = f"chore(sync): add {path} from {self.author}"
            content = self._encode(path, text, encrypted)
            resp = await self._call("PUT", path, {"message": message, "content": content})
            if resp.status_code == 422:  # a file with that name already exists
                continue
            if resp.is_error:
                raise ToolError(f"GitHub refused the note: HTTP {resp.status_code}")
            return path
        raise ToolError("Could not find a free file name for this note.")

    async def create(self, folder: str, title: str, text: str, today: str) -> str:
        """Commit ``text`` as a new file and return its path; never overwrites a file."""
        refuse_secrets(text)
        return await self._add(f"{folder}/{today}-{slugify(title)}", text)

    async def _read(self, path: str) -> tuple[str, str, bool]:
        """The file's version, text, and whether it was encrypted."""
        resp = await self._call("GET", path)
        if resp.is_error:
            raise ToolError(f"GitHub could not read {path}: HTTP {resp.status_code}")
        current = resp.json()
        text, encrypted = self._decode(current["content"])
        return current["sha"], text, encrypted

    async def text(self, path: str) -> str:
        return (await self._read(path))[1]

    async def files(self, folder: str, recursive: bool = True) -> list[str]:
        """The paths of the Markdown files of ``folder`` and of its sub-folders; [] when it
        does not exist."""
        resp = await self._call("GET", folder)
        if resp.status_code == 404:
            return []
        if resp.is_error:
            raise ToolError(f"GitHub could not list {folder}: HTTP {resp.status_code}")
        paths: list[str] = []
        for item in resp.json():
            if item.get("type") == "dir":
                if recursive:
                    paths += await self.files(item["path"])
            elif item.get("type") == "file" and item["path"].endswith(".md"):
                paths.append(item["path"])
        return paths

    async def put(self, path: str, text: str, why: str) -> None:
        """Write ``path``, a new file or in place of the one there, which keeps its format
        (encrypted or not)."""
        refuse_secrets(text)
        resp = await self._call("GET", path)
        body: dict[str, Any] = {"message": f"chore(sync): {why} {path} from {self.author}"}
        encrypted = False
        if resp.status_code != 404:
            if resp.is_error:
                raise ToolError(f"GitHub could not read {path}: HTTP {resp.status_code}")
            current = resp.json()
            body["sha"] = current["sha"]
            encrypted = ConfigCrypt.is_encrypted(base64.b64decode(current["content"]))
        body["content"] = self._encode(path, text, encrypted)
        resp = await self._call("PUT", path, body)
        if resp.is_error:
            raise ToolError(f"GitHub refused to write {path}: HTTP {resp.status_code}")

    async def delete(self, path: str) -> bool:
        """Remove a file for good; False when there is none."""
        resp = await self._call("GET", path)
        if resp.status_code == 404:
            return False
        if resp.is_error:
            raise ToolError(f"GitHub could not read {path}: HTTP {resp.status_code}")
        message = f"chore(sync): delete {path} from {self.author}"
        resp = await self._call("DELETE", path, {"message": message, "sha": resp.json()["sha"]})
        if resp.is_error:
            raise ToolError(f"GitHub refused to delete {path}: HTTP {resp.status_code}")
        return True

    async def close(self, path: str, closed_at: str) -> str:
        """Move a note to the ``past`` folder next to it with a ``closed:`` line, as
        the reminders format closes one."""
        sha, text, encrypted = await self._read(path)
        folder, _, name = path.rpartition("/")
        moved = await self._add(
            f"{folder}/past/{name.removesuffix('.md')}",
            with_field(text, "closed", closed_at),
            encrypted,
        )
        message = f"chore(sync): close {path} from {self.author}"
        resp = await self._call("DELETE", path, {"message": message, "sha": sha})
        if resp.is_error:
            raise ToolError(f"GitHub refused to close {path}: HTTP {resp.status_code}")
        return moved

    async def close_for_day(self, path: str, day: str) -> None:
        """Close a repeating note for ``day`` with a ``done:`` line, as the reminders format
        does; the note stays where it is for its next day."""
        sha, text, encrypted = await self._read(path)
        content = self._encode(path, with_field(text, "done", day), encrypted)
        message = f"chore(sync): close {path} for {day} from {self.author}"
        resp = await self._call("PUT", path, {"message": message, "content": content, "sha": sha})
        if resp.is_error:
            raise ToolError(f"GitHub refused to close {path} for {day}: HTTP {resp.status_code}")


class NoteFolder(Protocol):
    """One folder of Markdown notes, each named ``<id>.md``: where the tasks live."""

    async def named_files(self) -> dict[str, str]: ...

    async def text(self, name: str) -> str: ...

    async def create(self, title: str, text: str, today: str) -> str: ...

    async def put(self, name: str, text: str) -> None: ...

    async def delete(self, name: str) -> bool: ...

    async def close(self, name: str, closed_at: str) -> str: ...

    async def close_for_day(self, name: str, day: str) -> None: ...


class LocalFolder:
    """A folder of the agent's disk (DATA_DIR/tasks). Names come from the code (a date and
    the title's slug) or were checked by note_id: never a path a model wrote."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, name: str) -> Path:
        return self._dir / f"{note_id(name, 'list_scheduled_tasks')}.md"

    def _write(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError as exc:
            raise ToolError(f"{path.name} could not be saved: {exc}") from exc

    async def named_files(self) -> dict[str, str]:
        files = {}
        for path in sorted(self._dir.glob("*.md")) if self._dir.is_dir() else ():
            try:
                files[path.name] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return files

    async def text(self, name: str) -> str:
        try:
            return self._path(name).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"No note {name}.") from exc

    async def create(self, title: str, text: str, today: str) -> str:
        refuse_secrets(text)
        stem = f"{today}-{slugify(title)}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            name = f"{stem}.md" if attempt == 1 else f"{stem}-{attempt}.md"
            if not (self._dir / name).exists():
                self._write(self._dir / name, text)
                return name
        raise ToolError("Could not find a free file name for this note.")

    async def put(self, name: str, text: str) -> None:
        refuse_secrets(text)
        self._write(self._path(name), text)

    async def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.is_file():
            return False
        path.unlink()
        return True

    async def close(self, name: str, closed_at: str) -> str:
        text = await self.text(name)
        moved = self._dir / PAST / self._path(name).name
        for attempt in range(2, MAX_ATTEMPTS + 2):
            if not moved.exists():
                break
            moved = moved.with_name(f"{moved.stem.rsplit('--', 1)[0]}--{attempt}.md")
        self._write(moved, with_field(text, "closed", closed_at))
        self._path(name).unlink(missing_ok=True)
        return f"{PAST}/{moved.name}"

    async def close_for_day(self, name: str, day: str) -> None:
        self._write(self._path(name), with_field(await self.text(name), "done", day))


class RepoFolder:
    """A folder of a GitHub repository: read through ``reader`` (which downloads a file
    again only when it changed), written through ``repo``."""

    def __init__(self, repo: NotesRepo, reader: ReminderRepo, path: str) -> None:
        self._repo = repo
        self._reader = reader
        self._path = path.strip("/")

    def _file(self, name: str) -> str:
        return f"{self._path}/{note_id(name, 'list_scheduled_tasks')}.md"

    async def named_files(self) -> dict[str, str]:
        return await self._reader.named_files()

    async def text(self, name: str) -> str:
        return await self._repo.text(self._file(name))

    async def create(self, title: str, text: str, today: str) -> str:
        return (await self._repo.create(self._path, title, text, today)).rpartition("/")[2]

    async def put(self, name: str, text: str) -> None:
        await self._repo.put(self._file(name), text, "update")

    async def delete(self, name: str) -> bool:
        return await self._repo.delete(self._file(name))

    async def close(self, name: str, closed_at: str) -> str:
        return await self._repo.close(self._file(name), closed_at)

    async def close_for_day(self, name: str, day: str) -> None:
        await self._repo.close_for_day(self._file(name), day)


def with_field(text: str, key: str, value: str) -> str:
    """``text`` with ``key: value`` in its front matter, in place of the field when it is
    already there; unchanged when it has no front matter."""
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        return text
    end = text.index("\n---", 3)
    field = f"{key}: {value}"
    head = text[4:end].splitlines()
    if any(line.startswith(f"{key}:") for line in head):
        head = [field if line.startswith(f"{key}:") else line for line in head]
    else:
        head.append(field)
    return "---\n" + "\n".join(head) + text[end:]


def with_body(text: str, body: str) -> str:
    """``text`` with ``body`` in place of what follows its front matter."""
    if not text.startswith("---\n") or "\n---" not in text[3:]:
        raise ToolError("This file has no front matter to keep.")
    end = text.index("\n---", 3)
    return f"{text[:end]}\n---\n\n{body.strip()}\n"


def front_matter(fields: dict[str, str | None]) -> str:
    lines = [f"{key}: {one_line(value)}" for key, value in fields.items() if value]
    return "---\n" + "\n".join(lines) + "\n---\n"


