import ast
from pathlib import Path

import pytest

import intelligent_home_agent
from intelligent_home_agent.conversations import Attachment, ConversationStore
from intelligent_home_agent.main import with_waiting
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.proposals import build_proposal_tools


def test_every_write_that_waits_for_a_go_says_what_it_does_in_words():
    # The user is shown it: a sentence, never the tool's name and arguments.
    missing = []
    for file in Path(intelligent_home_agent.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Tool"):
                continue
            keywords = {k.arg for k in node.keywords}
            if keywords & {"proposable", "always_propose"} and "describe" not in keywords:
                missing.append(f"{file.name}:{node.lineno}")
    assert missing == []


def registry_with_a_write(done: list[dict], **options) -> ToolRegistry:
    async def save(args, _ctx):
        done.append(args)
        return {"saved_to": args["to_folder"]}

    registry = ToolRegistry(
        [Tool("save", "", {"type": "object"}, save, needs_clean_turn=True, proposable=True)]
    )
    registry.extend(build_proposal_tools(registry, **options))
    return registry


async def test_a_write_after_reading_waits_for_the_users_next_message(ctx):
    done: list[dict] = []
    registry = registry_with_a_write(done)
    ctx.tainted = True  # an email was read
    args = {"file_name": "F1.pdf", "to_folder": "Documents/Work/Invoices"}

    parked = await registry.run("save", args, ctx)
    again = await registry.run("save", args, ctx)
    with pytest.raises(ToolError, match="separate\\s+request"):
        await registry.run("run_proposals", {}, ctx)

    assert parked["status"] == "waiting_for_the_user" and again["status"] == "already_waiting"
    assert ctx.actions == [
        {
            "type": "proposal",
            "number": 1,
            "summary": "Save — file name: F1.pdf, to folder: Documents/Work/Invoices",
        }
    ]
    assert done == []

    clean = ToolContext(conversation=ctx.conversation)
    ctx.conversation.turn += 1  # the user answered
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await registry.run("run_proposals", {}, clean)  # never shown to them
    ctx.conversation.turn -= 1
    shown = with_waiting("Here.", ctx.actions, ctx.conversation)
    assert "Waiting for your go" in shown
    assert shown.endswith("1. Save — file name: F1.pdf, to folder: Documents/Work/Invoices")
    with pytest.raises(ToolError, match="not answered yet"):
        await registry.run("run_proposals", {}, clean)
    ctx.conversation.turn += 1
    result = await registry.run("run_proposals", {}, clean)

    assert done == [args]
    assert result == {
        "done": [{"number": 1, "result": {"saved_to": "Documents/Work/Invoices"}}],
        "failed": [],
    }
    assert ctx.conversation.proposals == []


async def test_the_list_under_the_reply_is_in_the_users_language(ctx):
    registry = registry_with_a_write([])
    ctx.tainted = True
    await registry.run("save", {"to_folder": "A"}, ctx)

    shown = with_waiting("Voilà.", ctx.actions, ctx.conversation, "fr")

    assert "En attente de ton accord" in shown and "« oui »" in shown
    assert with_waiting("Nothing waits.", [], ctx.conversation) == "Nothing waits."


async def test_only_the_users_own_message_runs_them_and_no_drops_them(ctx):
    done: list[dict] = []
    registry = registry_with_a_write(done)
    ctx.tainted = True
    for folder in ("A", "B", "C"):
        await registry.run("save", {"to_folder": folder}, ctx)
    with_waiting("", ctx.actions, ctx.conversation)
    ctx.conversation.turn += 1

    scheduled = ToolContext(conversation=ctx.conversation, scheduled=True)
    with pytest.raises(ToolError, match="scheduled task"):
        await registry.run("run_proposals", {}, scheduled)
    user = ToolContext(conversation=ctx.conversation)
    await registry.run("drop_proposals", {"numbers": [2]}, user)
    await registry.run("run_proposals", {"numbers": [3]}, user)

    assert done == [{"to_folder": "C"}]
    assert [p.number for p in ctx.conversation.proposals] == [1]


async def test_what_is_approved_is_exactly_what_runs(ctx):
    done: list[dict] = []
    registry = registry_with_a_write(done)
    ctx.tainted = True
    with pytest.raises(ToolError, match="hidden characters"):
        await registry.run("save", {"to_folder": "Documents‮/fdp.exe"}, ctx)
    with pytest.raises(ToolError, match="too long"):
        await registry.run("save", {"to_folder": "A" * 2000}, ctx)
    with pytest.raises(ToolError, match="hidden characters"):
        await registry.run("save", {"items": [{"title": "Quote\n\nTotal 0"}]}, ctx)

    conv = ctx.conversation
    conv.files["F1.pdf"] = Attachment("F1.pdf", "application/pdf", b"%PDF-approved", "email")
    await registry.run("save", {"file_name": "F1.pdf", "to_folder": "A"}, ctx)
    with_waiting("", ctx.actions, conv)
    conv.files["F1.pdf"] = Attachment("F1.pdf", "application/pdf", b"%PDF-swapped", "email")
    conv.turn += 1
    result = await registry.run("run_proposals", {}, ToolContext(conversation=conv))

    assert done == [] and "changed since" in result["failed"][0]["error"]


async def test_a_reply_quoting_a_message_parks_writes_but_yes_runs_the_list(ctx):
    done: list[dict] = []
    registry = registry_with_a_write(done)
    ctx.tainted = True
    await registry.run("save", {"to_folder": "A"}, ctx)
    with_waiting("", ctx.actions, ctx.conversation)
    ctx.conversation.turn += 1

    # "Yes" in reply to the list: the quote may hold an email's words.
    quoting = ToolContext(conversation=ctx.conversation, quoted=True)
    parked = await registry.run("save", {"to_folder": "B"}, quoting)
    result = await registry.run("run_proposals", {"numbers": [1]}, quoting)

    assert parked["status"] == "waiting_for_the_user"
    assert done == [{"to_folder": "A"}] and result["failed"] == []


async def test_an_approved_plan_runs_its_writes_unless_it_reads_private_content(ctx):
    done: list[dict] = []
    registry = registry_with_a_write(done)
    approved = (
        "Download October.pdf from payroll.example.com, file it in Work/Pay/2026, "
        "flag the email 4521"
    )
    plan = ToolContext(conversation=ctx.conversation, scheduled=True, approved_plan=approved)
    plan.read_page("payroll.example.com")
    assert plan.tainted

    saved = await registry.run(
        "save", {"file_name": "october.pdf", "to_folder": "Work/Pay/2026", "id": 4521}, plan
    )
    assert saved == {"saved_to": "Work/Pay/2026"}
    # A destination the plan never showed the user: a page could have put it there.
    elsewhere = await registry.run("save", {"to_folder": "Work/Pay/2025"}, plan)
    plan.tainted = True  # an email was read on the way
    parked = await registry.run("save", {"to_folder": "Work/Pay/2026"}, plan)

    assert [d["to_folder"] for d in done] == ["Work/Pay/2026"]
    assert elsewhere["status"] == parked["status"] == "waiting_for_the_user"
    with pytest.raises(ToolError, match="disabled"):
        await registry.run("run_proposals", {}, plan)


async def test_a_plan_always_waits_and_is_queued_once_the_user_agrees(ctx):
    registry = registry_with_a_write([])
    plan = {"plan": "Fetch the  payslip, file it in Work/Pay/2026"}

    parked = await registry.run("propose_plan", plan, ctx)  # a clean request, still parked

    assert parked["status"] == "waiting_for_the_user" and ctx.conversation.plans == []
    assert ctx.actions[0]["summary"] == "Plan: Fetch the payslip, file it in Work/Pay/2026"
    with_waiting("", ctx.actions, ctx.conversation)
    ctx.conversation.turn += 1
    result = await registry.run("run_proposals", {}, ToolContext(conversation=ctx.conversation))

    assert result["done"][0]["result"]["status"] == "starting"
    assert ctx.conversation.plans == ["Fetch the payslip, file it in Work/Pay/2026"]


async def test_a_plan_is_described_in_the_users_language(ctx):
    registry = registry_with_a_write([], language="fr")
    await registry.run("propose_plan", {"plan": "Ranger la facture"}, ctx)
    assert ctx.actions[0]["summary"] == "Plan : Ranger la facture"


async def test_nothing_waits_when_nobody_can_answer(ctx):
    done: list[dict] = []
    registry = registry_with_a_write(done)
    briefing = ToolContext(conversation=ctx.conversation, tainted=True, answerable=False)

    with pytest.raises(ToolError, match="nobody can answer"):
        await registry.run("save", {"to_folder": "A"}, briefing)

    assert done == [] and ctx.conversation.proposals == [] and briefing.actions == []


async def test_waiting_actions_are_listed_again_without_their_text_coming_back(ctx):
    registry = registry_with_a_write([])
    ctx.tainted = True
    await registry.run("save", {"to_folder": "A"}, ctx)
    await registry.run("save", {"to_folder": "B"}, ctx)
    with_waiting("", ctx.actions[:1], ctx.conversation)  # only the first went out

    later = ToolContext(conversation=ctx.conversation)
    result = await registry.run("show_waiting_proposals", {}, later)

    assert result == {"waiting": 1}
    assert later.actions == [{"type": "proposal", "number": 1, "summary": "Save — to folder: A"}]


async def test_a_poll_question_only_exists_where_a_chat_can_show_one(ctx):
    assert registry_with_a_write([]).get("ask_in_poll") is None
    registry = registry_with_a_write([], polls=True)

    with pytest.raises(ToolError, match="2 to 11"):
        await registry.run("ask_in_poll", {"question": "Which day?", "options": ["Mon"]}, ctx)
    asked = await registry.run(
        "ask_in_poll", {"question": "Which  day?", "options": ["Mon", "Tue", "Mon"]}, ctx
    )

    assert asked["status"] == "sent_with_your_reply"
    assert ctx.actions == [{"type": "question", "question": "Which day?"}]
    assert ctx.conversation.questions == [
        {
            "question": "Which day?",
            "options": ["Mon", "Tue"],
            "poll_id": "",
            "multiple": False,
            "clean": True,
        }
    ]


def test_reading_pages_taints_the_request_but_is_kept_apart(ctx):
    assert not ctx.tainted and not ctx.private_read
    ctx.read_page("App.Example.COM.")
    assert ctx.tainted and not ctx.private_read and ctx.page_hosts == {"app.example.com"}
    ctx.tainted = True
    assert ctx.private_read
    assert ToolContext(conversation=ctx.conversation, tainted=True).private_read


def test_a_failed_turn_leaves_nothing_to_approve(ctx):
    ctx.conversation.propose("save", {"to_folder": "A"}, "save(to_folder=A)")
    ctx.conversation.drop_unshown()
    assert ctx.conversation.proposals == []


def test_waiting_actions_outlive_a_restart(tmp_path, ctx):
    ctx.conversation.propose("save", {"to_folder": "A"}, "save(to_folder=A)")
    ctx.conversation.note_clean_reply("Sprint points:\n- Alex: 20")
    store = ConversationStore(3600)
    store.save(ctx.conversation, tmp_path / "chat.json")

    restored = ConversationStore(3600).restore(tmp_path / "chat.json")

    assert restored is not None
    # A reply to the agent's clean message still counts as the user's own after a restart.
    assert restored.said_cleanly("Sprint points: - Alex")
    assert [(p.number, p.args) for p in restored.proposals] == [(1, {"to_folder": "A"})]


async def test_a_tool_named_through_an_mcp_bridge_is_still_found(ctx):
    # An engine that reaches the tools through an MCP bridge shows the model
    # mcp__<server>__save, and the model may pass that name on.
    done: list[dict] = []
    registry = registry_with_a_write(done)
    args = {"file_name": "F1.pdf", "to_folder": "A"}

    parked = await registry.run(
        "propose_action", {"tool": "mcp__agent__save", "arguments": args}, ctx
    )
    saved = await registry.run("mcp__agent__save", args, ctx)

    assert parked["status"] == "waiting_for_the_user"
    assert ctx.conversation.proposals[0].tool == "save"
    assert saved == {"saved_to": "A"} and done == [args]


async def test_a_write_refused_after_a_read_still_waits_in_a_clean_request(ctx):
    # "Shall I leave you a reminder?" asked with propose_action, as the prompt says,
    # instead of a refusal.
    done: list[dict] = []

    async def remind(args, _ctx):
        done.append(args)
        return {"created": args["title"]}

    async def weather(_args, _ctx):
        return {}

    registry = ToolRegistry(
        [
            Tool("remind", "", {"type": "object"}, remind, needs_clean_turn=True),
            Tool("weather", "", {"type": "object"}, weather),
        ]
    )
    registry.extend(build_proposal_tools(registry))
    args = {"title": "Plug in a tool"}

    parked = await registry.run("propose_action", {"tool": "remind", "arguments": args}, ctx)

    assert parked["status"] == "waiting_for_the_user" and done == []
    assert "Waiting for your go" in with_waiting("Here.", ctx.actions, ctx.conversation)
    ctx.conversation.turn += 1  # the user says yes
    result = await registry.run("run_proposals", {}, ToolContext(conversation=ctx.conversation))
    assert done == [args] and result["done"][0]["result"] == {"created": "Plug in a tool"}

    with pytest.raises(ToolError, match="call it, or ask the user"):
        await registry.propose("weather", {}, ctx)  # nothing to wait for
    # In a reply quoting a message, or after an email, it waits for the go as well.
    quoted = ToolContext(conversation=ctx.conversation, quoted=True)
    assert (await registry.propose("remind", args, quoted))["status"] == "waiting_for_the_user"
    ctx.tainted = True
    assert (await registry.run("remind", args, ctx))["status"] == "already_waiting"
