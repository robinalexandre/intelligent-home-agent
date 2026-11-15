"""Writes parked in a request that read others' content, run on the user's go.

A request that read an email, an account or a page can't write: the text it read could be
steering it. A write that needs a clean request is parked instead, with its exact
arguments, and the code lists it, numbered, under the reply (a chat may also show it as a
poll). Only the user can run it, as parked: a later message of theirs, in a request that
read nothing else and isn't a scheduled task, or their vote in the poll. Nothing read
since can change what it does.
"""

from __future__ import annotations

from typing import Any

from intelligent_home_agent.conversations import file_hash
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, ToolRegistry

# What a chat's poll usually takes (12 options), one kept for the button that sends it.
MAX_QUESTION_OPTIONS = 11


def _numbers(args: dict[str, Any]) -> set[int] | None:
    numbers = args.get("numbers")
    if not numbers:
        return None
    try:
        return {int(n) for n in numbers}
    except (TypeError, ValueError) as exc:
        raise ToolError("numbers are the ones listed to the user, e.g. [1, 3].") from exc


def build_proposal_tools(
    registry: ToolRegistry, *, language: str = "en", polls: bool = False
) -> list[Tool]:
    """``polls``: the agent is reached through a chat that can show a poll and bring its
    answer back as the user's next message; ask_in_poll only exists then."""

    async def run_proposals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        conv = ctx.conversation
        wanted = _numbers(args)
        chosen = [
            p for p in conv.open_proposals() if p.shown and (wanted is None or p.number in wanted)
        ]
        if not chosen:
            raise ToolError("Nothing is waiting with these numbers (it may have expired).")
        if any(p.created_turn >= conv.turn for p in chosen):
            raise ToolError("The user has not answered yet: ask them and wait for their reply.")
        done, failed = [], []
        for proposal in chosen:
            conv.proposals.remove(proposal)
            changed = [
                name
                for name, digest in proposal.file_hashes.items()
                if name not in conv.files or file_hash(conv.files[name]) != digest
            ]
            if changed:
                failed.append({"number": proposal.number, "error": f"{changed} changed since."})
                continue
            try:
                result = await registry.run(proposal.tool, proposal.args, ctx, approved=True)
            except ToolError as exc:
                failed.append({"number": proposal.number, "error": str(exc)})
            else:
                done.append({"number": proposal.number, "result": result})
        return {"done": done, "failed": failed}

    async def drop_proposals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        wanted = _numbers(args)
        conv = ctx.conversation
        dropped = [p.number for p in conv.open_proposals() if wanted is None or p.number in wanted]
        conv.proposals = [p for p in conv.proposals if p.number not in dropped]
        return {"dropped": dropped}

    async def show_waiting_proposals(_args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # Listed again by the code under the reply; their text never comes back to the
        # model, as it was built from what others wrote.
        waiting = [p for p in ctx.conversation.open_proposals() if p.shown]
        for proposal in waiting:
            proposal.poll_id = proposal.option = ""
            ctx.actions.append(
                {"type": "proposal", "number": proposal.number, "summary": proposal.summary}
            )
        return {"waiting": len(waiting)}

    async def propose_action(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        arguments = args.get("arguments")
        if not isinstance(arguments, dict):
            raise ToolError("arguments are the action's own arguments, as an object.")
        return await registry.propose(str(args.get("tool") or ""), arguments, ctx)

    async def propose_plan(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # Parked by the registry (always_propose): this only runs on the user's go, and the
        # code then starts the plan as a request of its own once this reply is written.
        plan = " ".join(str(args.get("plan") or "").split())
        if not plan:
            raise ToolError("plan is required.")
        ctx.conversation.plans.append(plan)
        return {"status": "starting", "note": "The plan runs right after this reply."}

    async def describe_plan(args: dict[str, Any]) -> str:
        plan = " ".join(str(args.get("plan") or "").split())
        return strings(language).plan_summary.format(plan=plan)

    async def ask_in_poll(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        question = " ".join(str(args.get("question") or "").split())
        options = [" ".join(str(o).split()) for o in args.get("options") or []]
        options = [o for o in dict.fromkeys(options) if o]
        if not question or not 2 <= len(options) <= MAX_QUESTION_OPTIONS:
            raise ToolError(f"A question and 2 to {MAX_QUESTION_OPTIONS} different options.")
        if len(question) > 250 or any(len(o) > 100 for o in options):
            raise ToolError("The question takes 250 characters at most, an option 100.")
        multiple = bool(args.get("multiple"))
        # Asked before this request read anything of others': its words are the agent's
        # own, from the user's, so the answer is the user's own request.
        clean = not (ctx.tainted or ctx.quoted)
        ctx.conversation.questions.append(
            {
                "question": question,
                "options": options,
                "poll_id": "",
                "multiple": multiple,
                "clean": clean,
            }
        )
        ctx.actions.append({"type": "question", "question": question})
        how = "ticks, then sends," if multiple else "taps one option"
        return {
            "status": "sent_with_your_reply",
            "note": f"Stop and wait: the user {how} and their choice comes back as their "
            "next message.",
        }

    numbers = {
        "type": "array",
        "items": {"type": "integer"},
        "description": "The numbers listed to the user; leave out for all of them.",
    }
    tools = [
        Tool(
            name="run_proposals",
            description=(
                "Do the actions waiting for the user's go (listed, numbered, under your "
                "earlier reply), exactly as listed, when the user's message agrees: all of "
                "them, or the numbers they chose. Don't read anything before: do it first."
            ),
            input_schema={"type": "object", "properties": {"numbers": numbers}},
            handler=run_proposals,
            needs_clean_turn=True,
            never_park=True,
            needs_user_request=True,
            runs_shown_writes=True,
        ),
        Tool(
            name="propose_action",
            description=(
                "Ask the user before an action instead of doing it: a yes/no question about "
                "deleting, moving, changing... The action, with its exact arguments, waits "
                "for their go, shown to them under your reply, and runs exactly as shown."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "description": "The action's tool name."},
                    "arguments": {"type": "object"},
                },
                "required": ["tool", "arguments"],
            },
            handler=propose_action,
        ),
        Tool(
            name="propose_plan",
            description=(
                "When what you read (an email, a page) calls for several steps, propose the "
                "whole plan in one line instead of asking a go for each step: what to "
                "fetch and where, and every argument of each write as you will pass it "
                "(names, paths, ids). It waits for the user's go with the other actions; "
                "once approved, it runs as your own request, all steps in a row, without "
                "another go for the writes it spelled out."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "The steps, in the user's language, one line.",
                    }
                },
                "required": ["plan"],
            },
            handler=propose_plan,
            describe=describe_plan,
            proposable=True,
            always_propose=True,
        ),
        Tool(
            name="show_waiting_proposals",
            description=(
                "List again, under your reply, every action still waiting for the user's "
                "go (kept 14 days), e.g. for a weekly recap."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=show_waiting_proposals,
        ),
        Tool(
            name="drop_proposals",
            description="Forget actions waiting for the user's go, when they say no.",
            input_schema={"type": "object", "properties": {"numbers": numbers}},
            handler=drop_proposals,
        ),
    ]
    if polls:
        tools.append(
            Tool(
                name="ask_in_poll",
                description=(
                    "Ask the user a question as a poll they tap instead of typing: which "
                    "day, which option, yes or no. One tap on an option answers; with "
                    "multiple, they tick several and send. They may instead answer in "
                    "writing. Use it whenever a question has a few clear answers."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "multiple": {
                            "type": "boolean",
                            "description": "Several options may apply (default: exactly one).",
                        },
                    },
                    "required": ["question", "options"],
                },
                handler=ask_in_poll,
            )
        )
    return tools
