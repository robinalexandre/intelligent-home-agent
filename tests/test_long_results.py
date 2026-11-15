from conftest import FakeLLM, text_response, tool_response

from intelligent_home_agent.agent import Agent
from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.prompts import PromptConfig
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.long_results import (
    MAX_KEPT,
    PART_CHARS,
    LongResult,
    build_long_result_tools,
    keep_long,
)

LONG = "".join(f"line {n:05d} of the contract. " for n in range(3000))  # ~81 000 characters
PARTS = -(-len(LONG) // PART_CHARS)


def make_agent(fake: FakeLLM, registry: ToolRegistry) -> Agent:
    return Agent(
        fake,
        registry,
        PromptConfig("Nova", "en", "UTC"),
        model="test-model",
        max_tokens=256,
        max_tool_iterations=4,
        max_history_messages=40,
    )


def registry_with(**flags):
    async def read_mail(_args, _ctx):
        return LONG

    async def note(_args, _ctx):
        return {"ok": True}

    registry = ToolRegistry(
        [
            Tool("read_mail", "", {"type": "object"}, read_mail, **flags),
            Tool("note", "", {"type": "object"}, note, needs_clean_turn=True, never_park=True),
        ]
    )
    registry.extend(build_long_result_tools())
    return registry


async def test_a_long_result_comes_in_parts_and_the_end_is_not_lost():
    fake = FakeLLM(
        [
            tool_response(("t1", "read_mail", {})),
            tool_response(("t2", "read_result_part", {"id": "r1", "part": PARTS})),
            text_response("The end of the contract says…"),
        ]
    )
    conv = Conversation(id="c")
    await make_agent(fake, registry_with()).ask(conv, "Read the contract")

    first = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert first.startswith(LONG[:100]) and len(first) < PART_CHARS + 400
    assert f"part 1 of {PARTS}" in first and "read_result_part with id r1" in first
    last = fake.requests[2]["messages"][-1]["content"][0]["content"]
    assert last.endswith(LONG[-60:]) and f"part {PARTS} of {PARTS}" in last


async def test_a_part_of_an_email_taints_the_request_that_reads_it():
    conv = Conversation(id="c")
    fake = FakeLLM([tool_response(("t1", "read_mail", {})), text_response("Read.")])
    await make_agent(fake, registry_with(taints=True)).ask(conv, "Read the email")
    # A later request reads the rest: it read the email too, so it can't act.
    fake = FakeLLM(
        [
            tool_response(("t1", "read_result_part", {"id": "r1", "part": 2})),
            tool_response(("t2", "note", {})),
            text_response("Done."),
        ]
    )
    await make_agent(fake, registry_with(taints=True)).ask(conv, "And the rest?")
    refused = fake.requests[2]["messages"][-1]["content"][0]
    assert refused["is_error"] and "disabled" in refused["content"]


async def test_a_part_carries_what_its_whole_was_for_the_request(ctx):
    (tool,) = build_long_result_tools()
    conv = ctx.conversation
    page = keep_long(conv, LongResult("open_page", "x" * 30_000, hosts=frozenset({"example.com"})))
    search = keep_long(conv, LongResult("web_search", "y" * 30_000, web=True))

    reading = ToolContext(conversation=conv)
    text = await tool.handler({"id": page, "part": 3}, reading)
    assert text.startswith(f"[Result {page}, part 3 of 3]") and len(text) < 7_000
    assert reading.page_hosts == {"example.com"} and not reading.private_read
    await tool.handler({"id": search, "part": 2}, reading)
    assert reading.web_read and not reading.private_read


async def test_a_part_that_does_not_exist_is_refused_and_old_results_are_dropped(ctx):
    (tool,) = build_long_result_tools()
    conv = ctx.conversation
    keys = [keep_long(conv, LongResult("t", "z" * 13_000)) for _ in range(MAX_KEPT + 1)]

    assert keys[0] == "r1" and keys[0] not in conv.long_results
    assert len(conv.long_results) == MAX_KEPT
    for args, message in (
        ({"id": "r1", "part": 2}, "No kept result r1"),
        ({"id": keys[-1], "part": 3}, "parts 1 to 2"),
        ({"id": keys[-1], "part": "two"}, "part is a number"),
    ):
        try:
            await tool.handler(args, ctx)
        except ToolError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(args)
