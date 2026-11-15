import asyncio
import json
from datetime import date

import pytest
from conftest import FakeLLM, text_response

from intelligent_home_agent.curation import LLMRunner, available, curate_lessons


class FakeRepo:
    def __init__(self, files: dict[str, str]) -> None:
        self.files_ = dict(files)
        self.written: list[tuple[str, str]] = []

    async def files(self, folder):
        return [p for p in self.files_ if p.startswith(f"{folder}/")]

    async def text(self, path):
        return self.files_[path]

    async def put(self, path, text, why):
        self.written.append((path, why))
        self.files_[path] = text


NOTE = "---\nname: gateway\ntitle: Chat gateway\ntype: fact\ntopics: [chat]\n---\n\nIt runs.\n"
UNTOUCHED = "---\nname: backups\ntitle: Where the backups go\ntype: fact\n---\n\nThe NAS.\n"


async def test_the_lessons_become_notes_written_back_to_the_repo():
    pytest.importorskip("kb.curate")  # the "curation" extra
    repo = FakeRepo({"knowledge/gateway.md": NOTE, "knowledge/backups.md": UNTOUCHED})
    seen = []

    def runner(system, material):
        seen.append(material)
        return json.dumps(
            [
                {
                    "action": "merge",
                    "name": "gateway",
                    "title": "Quotes",
                    "sources": ["nightly-review"],
                    "body": "A reply quotes a message by its id.",
                },
                {
                    "action": "create",
                    "name": "agent-progress-lines",
                    "title": "Two progress lines at most",
                    "type": "decision",
                    "trust": "agent",
                    "topics": ["agent"],
                    "sources": ["nightly-review"],
                    "body": "The assistant sends at most two progress lines per request.",
                },
            ]
        )

    report = await curate_lessons(repo, runner, date(2026, 10, 24), "- Quotes need the id.")

    assert "gateway — Chat gateway" in seen[0] and "- Quotes need the id." in seen[0]
    assert report.created == ["agent-progress-lines: Two progress lines at most"]
    assert sorted(repo.written) == [
        ("knowledge/.curator/2026-10-24-nightly-review.md", "add"),
        ("knowledge/agent-progress-lines.md", "add"),
        ("knowledge/gateway.md", "curate"),
    ]
    assert "A reply quotes a message by its id." in repo.files_["knowledge/gateway.md"]
    assert repo.files_["knowledge/backups.md"] == UNTOUCHED


async def test_the_curator_calls_the_agents_own_model_from_its_worker_thread():
    llm = FakeLLM([text_response("[]")])
    runner = LLMRunner(llm, "some-model", asyncio.get_running_loop(), max_tokens=100)

    assert await asyncio.to_thread(runner, "You curate.", "- A lesson.") == "[]"
    [call] = llm.requests
    assert call["model"] == "some-model" and call["system"] == "You curate."
    assert call["messages"] == [{"role": "user", "content": "- A lesson."}]
    assert "tools" not in call and call["max_tokens"] == 100


def test_available_says_whether_the_kit_is_installed():
    assert isinstance(available(), bool)
