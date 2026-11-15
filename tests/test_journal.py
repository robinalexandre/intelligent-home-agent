import json
from datetime import date

from conftest import FakeLLM, text_response, tool_response

from intelligent_home_agent.agent import Agent
from intelligent_home_agent.conversations import Attachment, Conversation
from intelligent_home_agent.journal import Journal
from intelligent_home_agent.prompts import PromptConfig
from intelligent_home_agent.tools import build_home_assistant_tools
from intelligent_home_agent.tools.base import Tool, ToolError, ToolRegistry


def make_agent(fake, registry, journal):
    return Agent(
        fake,
        registry,
        PromptConfig("Nova", "en", "UTC"),
        model="test-model",
        max_tokens=256,
        max_tool_iterations=4,
        max_history_messages=40,
        journal=journal,
    )


def only_day(tmp_path):
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text().splitlines()]


async def test_a_turn_is_journaled_with_its_tools_errors_and_reply(tmp_path, ha, policy):
    fake = FakeLLM(
        [
            tool_response(("t1", "get_entity_state", {"entity_id": "switch.secret"})),
            text_response("I can't reach that device."),
        ]
    )
    journal = Journal(tmp_path, "UTC")
    agent = make_agent(fake, ToolRegistry(build_home_assistant_tools(ha, policy)), journal)

    await agent.ask(Conversation(id="c"), "State of the secret switch?", "Reminders: bread")

    [entry] = only_day(tmp_path)
    assert entry["display"] == "voice" and entry["outcome"] == "ok"
    assert entry["request"] == "State of the secret switch?"  # the phone's context is left out
    [call] = entry["tools"]
    assert call["name"] == "get_entity_state"
    assert json.loads(call["args"]) == {"entity_id": "switch.secret"}
    assert call["error"]
    assert entry["reply"] == "I can't reach that device."
    assert entry["reply_chars"] == len(entry["reply"]) and "tainted" not in entry


async def test_a_turn_that_read_others_content_keeps_no_reply_nor_arguments(tmp_path):
    async def refuse(_args, _ctx):
        raise ToolError("Waits for the user's go.")

    fake = FakeLLM(
        [tool_response(("t1", "act", {"to": "lena@example.com"})), text_response("Quote ready.")]
    )
    registry = ToolRegistry([Tool("act", "", {"type": "object"}, refuse)])
    agent = make_agent(fake, registry, Journal(tmp_path, "UTC"))
    pdf = Attachment("quote.pdf", "application/pdf", b"%PDF-1.7")

    await agent.ask(Conversation(id="c"), "Answer Lena", display="chat", files=[pdf])

    [entry] = only_day(tmp_path)
    assert entry["tainted"] is True and entry["files"] == ["quote.pdf"]
    assert entry["tools"] == [{"name": "act", "error": "Waits for the user's go."}]
    assert "reply" not in entry and entry["reply_chars"] == len("Quote ready.")


async def test_progress_lines_and_a_fallback_are_journaled(tmp_path):
    fake = FakeLLM([text_response("")])
    agent = make_agent(fake, ToolRegistry([]), Journal(tmp_path, "UTC"))

    async def progress(_text):
        pass

    await agent.ask(Conversation(id="c"), "x", display="chat", progress=progress)

    [entry] = only_day(tmp_path)
    assert entry["outcome"] == "fallback" and entry["progress"] == 0


async def test_a_turn_that_fails_is_journaled_as_an_error(tmp_path):
    class Broken:
        async def create(self, **_kwargs):
            raise RuntimeError("model down")

    agent = make_agent(Broken(), ToolRegistry([]), Journal(tmp_path, "UTC"))
    conv = Conversation(id="c")

    try:
        await agent.ask(conv, "Hello")
    except RuntimeError:
        pass

    [entry] = only_day(tmp_path)
    assert entry["outcome"] == "error: RuntimeError" and entry["reply"] == ""
    assert conv.messages == []


def test_a_day_is_read_back_and_old_days_are_removed(tmp_path):
    journal = Journal(tmp_path, "UTC", keep_days=30)
    (tmp_path / "2026-09-01.jsonl").write_text("{}\n")
    journal.add({"at": "2026-10-24T09:12:00+02:00", "request": "Hello"})
    with (tmp_path / "2026-10-24.jsonl").open("a") as fh:
        fh.write("not json\n")

    assert journal.day(date(2026, 10, 24)) == [
        {"at": "2026-10-24T09:12:00+02:00", "request": "Hello"}
    ]
    assert journal.day(date(2026, 10, 23)) == []
    assert not (tmp_path / "2026-09-01.jsonl").exists()
