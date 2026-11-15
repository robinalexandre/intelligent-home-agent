from types import SimpleNamespace as NS

import pytest
from conftest import FakeLLM, text_response

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.llm import LLMResponse
from intelligent_home_agent.tools import ToolError, build_code_tools


class Content:
    def __init__(self, data):
        self.data = data

    async def read(self):
        return self.data


class FakeFiles:
    def __init__(self, name="../outputs/chart.png", size=4):
        self.uploaded = []
        self.name, self.size = name, size

    async def upload(self, file):
        self.uploaded.append(file[0])
        return NS(id=f"in_{len(self.uploaded)}")

    async def retrieve_metadata(self, file_id):
        return NS(filename=self.name, size_bytes=self.size, mime_type="image/png")

    async def download(self, file_id):
        return Content(b"\x89PNG")


def run_blocks(*file_ids):
    """What the provider answers, as the dict blocks LLMClient returns."""
    return [
        {"type": "text", "text": "Computing."},
        {
            "type": "bash_code_execution_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": {
                "type": "bash_code_execution_result",
                "stdout": "",
                "content": [{"type": "bash_code_execution_output", "file_id": i} for i in file_ids],
            },
        },
        {"type": "text", "text": "Monthly payment: 1060.66."},
    ]


async def test_code_runs_in_the_sandbox_and_its_chart_is_kept_and_sent(ctx):
    llm, files = FakeLLM([LLMResponse("end_turn", run_blocks("out_1"))]), FakeFiles()
    (tool,) = build_code_tools(llm, files, "some-model", language="fr")
    ctx.conversation.add_file(Attachment("statement.csv", "text/csv", b"a,b\n1,2", origin="mail"))

    result = await tool.handler(
        {
            "task": "Monthly payment for 100,000 over 10 years at 5%, and a chart",
            "file_names": ["statement.csv"],
        },
        ctx,
    )

    assert result["answer"] == "Monthly payment: 1060.66."
    assert result["files"] == ["chart.png"]  # the path the sandbox gave is never used
    assert ctx.conversation.files["chart.png"].data == b"\x89PNG"
    assert ctx.conversation.files["chart.png"].origin == "agent"
    assert {"type": "share_file", "name": "chart.png"} in ctx.actions
    request = llm.requests[0]
    assert request["model"] == "some-model"
    assert request["tools"] == [{"type": "code_execution_20260521", "name": "code_execution"}]
    assert {"type": "container_upload", "file_id": "in_1"} in request["messages"][0]["content"]
    assert "Answer in French" in request["system"] and "labels in French" in request["system"]
    assert files.uploaded == ["statement.csv"]
    assert ctx.tainted  # a mailed file went in: what comes out may repeat it


async def test_a_task_on_the_agents_own_numbers_reads_nothing(ctx):
    llm = FakeLLM([text_response("42")])
    (tool,) = build_code_tools(llm, FakeFiles(), "some-model")

    result = await tool.handler({"task": "6 times 7"}, ctx)

    assert result == {"answer": "42"}
    assert not ctx.tainted and ctx.actions == []


async def test_a_paused_run_is_sent_back_until_it_ends(ctx):
    paused = LLMResponse("pause_turn", [{"type": "text", "text": "Still running."}])
    llm = FakeLLM([paused, LLMResponse("end_turn", run_blocks())])
    (tool,) = build_code_tools(llm, FakeFiles(), "some-model")

    result = await tool.handler({"task": "a long sum"}, ctx)

    assert result["answer"] == "Monthly payment: 1060.66."
    assert llm.requests[1]["messages"][-1] == {"role": "assistant", "content": paused.content}


async def test_a_file_too_big_or_without_a_name_is_left_in_the_sandbox(ctx):
    for files in (FakeFiles(size=11 * 1024 * 1024), FakeFiles(name="..")):
        llm = FakeLLM([LLMResponse("end_turn", run_blocks("out_1"))])
        (tool,) = build_code_tools(llm, files, "some-model")
        result = await tool.handler({"task": "a chart"}, ctx)
        assert "files" not in result and ctx.conversation.files == {}


async def test_what_cant_run_is_refused(ctx):
    (tool,) = build_code_tools(FakeLLM([]), FakeFiles(), "some-model")
    with pytest.raises(ToolError, match="what to compute"):
        await tool.handler({"task": " "}, ctx)
    with pytest.raises(ToolError, match="No saved file"):
        await tool.handler({"task": "sum it", "file_names": ["absent.csv"]}, ctx)


def test_a_provider_without_a_sandbox_has_no_code_tool():
    assert build_code_tools(FakeLLM([]), None, "some-model") == []
    assert build_code_tools(None, FakeFiles(), "some-model") == []
