"""The knowledge base's curator, run on the server for what the agent learned itself.

With claude-code-kit, each computer curates its own Claude Code sessions (kb, the kit's
public tool); the server curates, the same way, the lessons its nightly review drew from
the agent's requests (intelligent_home_agent.review). kb's ``curate_material`` gets them
with the notes of a folder of a notes repository (REVIEW_LESSONS_REPO, the one the
knowledge base indexes), copied into a temporary folder, and proposes notes; kb checks
them and writes the files there. What it created or completed (and its report) is then
written back to the repository, encrypted when that folder is, where the kb service and
the user's computers find it at their next pull.

Optional: it needs the ``curation`` extra (the kit's kb package). Without it, or without
REVIEW_LESSONS_REPO, the review's lessons stay in its final message and nothing is curated.
The curator's model call goes through the agent's own ``LLMClient``, so any provider works.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from intelligent_home_agent.llm import LLMClient

log = logging.getLogger(__name__)

NOTES_FOLDER = "knowledge"
MATERIAL_NAME = "nightly-review"
MAX_TOKENS = 8000


def available() -> bool:
    """Whether the kit's kb package is installed (the ``curation`` extra)."""
    return importlib.util.find_spec("kb") is not None


class NotesRepo(Protocol):
    async def files(self, folder: str) -> list[str]: ...

    async def text(self, path: str) -> str: ...

    async def put(self, path: str, text: str, why: str) -> None: ...


class LLMRunner:
    """kb's runner on the agent's model: one call, no tools, the answer's text. Pass a
    ``SubscriptionFirst`` for the Claude subscription to take it first (engine.py)."""

    def __init__(
        self,
        llm: LLMClient,
        model: str,
        loop: asyncio.AbstractEventLoop,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._llm = llm
        self._model = model
        # The server's event loop: kb calls the runner from a worker thread.
        self._loop = loop
        self._max_tokens = max_tokens

    def __call__(self, system: str, material: str) -> str:
        response = asyncio.run_coroutine_threadsafe(
            self._llm.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": material}],
            ),
            self._loop,
        ).result()
        return "".join(b.get("text", "") for b in response.content if b.get("type") == "text")


async def curate_lessons(
    repo: NotesRepo,
    runner: Any,
    day: date,
    lessons: str,
    *,
    folder: str = NOTES_FOLDER,
    name: str = MATERIAL_NAME,
) -> Any:
    """kb's report (created, merged, dropped), once the notes are written back."""
    folder = folder.strip("/")
    before = {
        path.removeprefix(f"{folder}/"): await repo.text(path) for path in await repo.files(folder)
    }
    report, after = await asyncio.to_thread(_curate, before, name, lessons, day, runner)
    for relative, text in sorted(after.items()):
        if before.get(relative) != text:
            why = "curate" if relative in before else "add"
            await repo.put(f"{folder}/{relative}", text, why)
    log.info(
        "Curated the %s of %s: %d created, %d merged, %d dropped",
        name,
        day,
        len(report.created),
        len(report.merged),
        len(report.dropped),
    )
    return report


def _curate(
    notes: dict[str, str], name: str, lessons: str, day: date, runner: Any
) -> tuple[Any, dict[str, str]]:
    """kb's curator on a copy of the notes; every Markdown file there once it is done."""
    from kb.curate import curate_material

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for relative, text in notes.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        report = curate_material(name, lessons, day, root, runner)
        after = {
            file.relative_to(root).as_posix(): file.read_text(encoding="utf-8")
            for file in root.rglob("*.md")
        }
    return report, after
