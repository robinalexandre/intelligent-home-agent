"""The agent in a chat, whichever app carries it (``chat.ChatChannel``).

One session per channel, each with its own conversation, kept across restarts. It answers
the user's messages, says a line now and then while a long request runs, and handles what
waits for the user's go the way a chat allows: the writes parked in a reply are sent as a
choice the user ticks (a poll, buttons), and only what they ticked and sent runs, exactly
as shown. A question the agent asks with ``ask_in_poll`` comes back as the user's answer.

The rules are the registry's (tools/base.py), unchanged: a write parked after a read can
only run through ``run_proposals``, for proposals the code listed in a reply that went
out, on the user's own go. A tick in the choice is that go. Nothing read since can change
what runs, and what the go's own writes read taints the request that picks up after it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from intelligent_home_agent.agent import AgentReply
from intelligent_home_agent.chat import DONE, FAILED, ChatChannel, same_message
from intelligent_home_agent.conversations import MAX_OPEN_QUESTIONS, Attachment, Conversation
from intelligent_home_agent.deliver import DeliveryError
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.progress import Tracker
from intelligent_home_agent.tasks import SILENT_REPLY, says_nothing
from intelligent_home_agent.tools.base import ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

# What the agent says while it works, sent as messages of their own: more is a flood. A
# long request of the user's own gets a few more than a run nobody is waiting for.
MAX_PROGRESS_MESSAGES = 2
MAX_DIRECT_PROGRESS_MESSAGES = 6
# The user's own request still running after this long without a word: the code says so.
HEARTBEAT_SECONDS = 5 * 60
# How a plan the user approved comes back to the agent, as a request of its own.
PLAN_TAG = "[Plan you proposed and the user approved as shown; do it now as their request:"
# How the agent learns that the writes it waited for ran on the user's go.
RESUME_TAG = "[The user gave their go in the chat:"
MAX_RESUME_CHARS = 30_000


class AgentLike(Protocol):
    @property
    def tools(self) -> ToolRegistry: ...

    async def ask(self, conv: Conversation, text: str, *args: Any, **kwargs: Any) -> AgentReply: ...


class WithWaiting(Protocol):
    def __call__(
        self, text: str, actions: list[dict], conv: Conversation, option_chars: int | None = None
    ) -> str: ...


def plain(text: str) -> str:
    """Text the agent wrote earlier, coming back: one line, without brackets that could
    pass for the code's own notes."""
    return "".join(c for c in " ".join(text.split()) if c.isprintable() and c not in "[]«»")


def too_long_for_option(number: int, summary: str, option_chars: int) -> bool:
    return len(f"{number}. {summary}") > option_chars


def choice_options(
    proposals: list[tuple[int, str]], option_chars: int, language: str = "en"
) -> list[str]:
    """Each write as the user reads it, numbered only when two read the same. One too long
    for an option is only a pointer to its full line in the message: never a cut one."""
    counts = Counter(summary for _, summary in proposals)
    see_above = strings(language).poll_see_above
    return [
        summary
        if counts[summary] == 1 and len(summary) <= option_chars
        else see_above.format(number=number)
        if too_long_for_option(number, summary, option_chars)
        else f"{number}. {summary}"
        for number, summary in proposals
    ]


def _multiple(question: dict) -> bool:
    """Several options may be ticked, so the answer ends with the send option; a
    single-choice question is answered by the first tap."""
    return bool(question.get("multiple"))


class Said:
    """The chat while a request runs: when the user last heard from it, and a line from
    the code each time it stays silent too long."""

    def __init__(self, send: Callable[[str], Awaitable[Any]]) -> None:
        self._send = send
        self._loop = asyncio.get_running_loop()
        self.started = self.last = self._loop.time()

    async def send(self, text: str) -> None:
        self.last = self._loop.time()
        await self._send(text)

    async def keep_alive(self, line: str, silence: float) -> None:
        while True:
            await asyncio.sleep(max(0.0, self.last + silence - self._loop.time()))
            if self._loop.time() - self.last >= silence:
                minutes = round((self._loop.time() - self.started) / 60)
                try:
                    await self.send(line.format(minutes=minutes))
                except Exception:
                    log.warning("Could not send the still-working line", exc_info=True)
                    self.last = self._loop.time()


def _first_ones(
    send: Callable[[str], Awaitable[None]], limit: int
) -> Callable[[str], Awaitable[None]]:
    sent = 0

    async def progress(text: str) -> None:
        nonlocal sent
        if sent < limit:
            sent += 1
            await send(text)

    return progress


class ChatSession:
    """What a channel talks to (``chat.ChatHandler``), and what the rest of the code uses
    to say something in that chat."""

    def __init__(
        self,
        channel: ChatChannel,
        agent: AgentLike,
        *,
        conversation: Callable[[], Conversation],
        save: Callable[[Conversation], None],
        with_waiting: WithWaiting,
        language: str = "en",
        vote_grace_seconds: float = 5,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        tracker: Tracker | None = None,
    ) -> None:
        self.channel = channel
        self.agent = agent
        self._conversation = conversation
        self._save = save
        self._with_waiting = with_waiting
        self._language = language
        self._s = strings(language)
        self._grace = vote_grace_seconds
        self._heartbeat = heartbeat_seconds
        # For features that start long work in the background (progress.py).
        self.tracker = tracker
        # One-tap answers waiting out their grace, and the questions answered lately.
        self.pending_votes: dict[str, asyncio.Task] = {}
        self.answered: dict[str, tuple[dict, list[str]]] = {}

    @property
    def name(self) -> str:
        return self.channel.name

    def conversation(self) -> Conversation:
        return self._conversation()

    # --- chat.ChatHandler --------------------------------------------------------------

    async def answer(self, text: str, files: list[Attachment] | None, quoted: str) -> str:
        return await self.ask(text, files, quoted=quoted) or ""

    async def after_reply(self) -> None:
        """Once a reply went out: its choices, then the plans the user approved in it."""
        await self.send_choices()
        await self.run_plans()

    async def chose(self, choice_id: str, selected: list[str]) -> None:
        """Once the user ticks the send option, run exactly the writes they ticked; the
        cancel option drops the choice's writes. Answered under the choice."""
        if await self._answer_question_choice(choice_id, selected):
            return
        s, channel = self._s, self.channel
        conv = self.conversation()
        async with conv.lock:
            in_choice = [
                p for p in conv.open_proposals() if p.shown and same_message(p.poll_id, choice_id)
            ]
            if not in_choice or not {s.poll_validate, s.poll_cancel} & set(selected):
                return
            choice_id = in_choice[0].poll_id
            if s.poll_cancel in selected:
                conv.proposals = [p for p in conv.proposals if p not in in_choice]
                conv.messages += [
                    {"role": "user", "content": "[The user cancelled the actions of the choice]"},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": s.poll_cancelled_note}],
                    },
                ]
                self._save(conv)
                await channel.mark(choice_id, FAILED)
                await channel.send(s.poll_cancelled, reply_to=choice_id)
                return
            voted = [p for p in in_choice if p.option in selected]
            if not voted:
                # Only the send option ticked; ticked lines already done are a repeat of
                # the same vote.
                if set(selected) <= {s.poll_validate, s.poll_cancel}:
                    await channel.send(s.poll_nothing_ticked, reply_to=choice_id)
                return
            numbers = [p.number for p in voted]
            summaries = {p.number: p.summary for p in conv.proposals}
            conv.turn += 1  # a vote is the user's own answer
            go = ToolContext(conversation=conv)
            try:
                result = await self.agent.tools.run("run_proposals", {"numbers": numbers}, go)
            except ToolError as exc:
                result = {"done": [], "failed": [{"number": n, "error": str(exc)} for n in numbers]}
            finally:
                await go.finish()
            lines = [
                s.go_done.format(summary=summaries[d["number"]], note=self._noted(d["result"]))
                for d in result["done"]
            ]
            lines += [
                s.go_failed.format(summary=summaries[f["number"]], error=f["error"])
                for f in result["failed"]
            ]
            # What the go itself asked again (something that changed since): a new choice.
            asked = [a for a in go.actions if a.get("type") == "proposal"]
            report = self._with_waiting("\n".join(lines), asked, conv, channel.option_chars)
            conv.messages += [
                {"role": "user", "content": f"[The user approved in the chat: {numbers}]"},
                {"role": "assistant", "content": [{"type": "text", "text": report}]},
            ]
            self._save(conv)
        await channel.mark(choice_id, FAILED if result["failed"] else DONE)
        await channel.send(report, reply_to=choice_id)
        await self.share_files(conv, go.actions)
        if asked:
            await self.send_choices()
        # An approved plan already runs as a request of its own (run_plans).
        if any(p.tool != "propose_plan" for p in voted):
            await self.resume_after_go(result, summaries, read=go.tainted)
        await self.run_plans()

    # --- asking the agent --------------------------------------------------------------

    async def ask(
        self,
        text: str,
        files: list[Attachment] | None = None,
        *,
        context: str | None = None,
        scheduled: bool = False,
        quoted: bool | str = False,
        tainted: bool = False,
        approved_plan: str = "",
        may_be_silent: bool = False,
    ) -> str | None:
        """Answer a message written in the chat, with the files it carries; what waits for
        the user's go is listed under the reply by the code.

        ``scheduled``: not a message the user just wrote (a briefing, an approved plan).
        ``approved_plan``: the plan the agent proposed and the user approved as shown.
        ``may_be_silent``: the agent may answer SILENT_REPLY (picking up after the user's
        go, with nothing left to do); None is returned and the chat keeps no trace of it.
        """
        conv = self.conversation()
        channel = self.channel
        # The user's own message, or a plan they approved from one: someone is waiting.
        direct = not scheduled or bool(approved_plan)
        said = Said(channel.send)
        limit = MAX_DIRECT_PROGRESS_MESSAGES if direct else MAX_PROGRESS_MESSAGES
        progress = _first_ones(said.send, limit)
        heartbeat = None
        if direct and self._heartbeat:
            heartbeat = asyncio.create_task(
                said.keep_alive(self._s.chat_still_working, self._heartbeat)
            )
        async with conv.lock:
            before = list(conv.messages)
            try:
                reply = await self.agent.ask(
                    conv,
                    text,
                    context,
                    display="chat",
                    files=files,
                    progress=progress,
                    scheduled=scheduled,
                    tainted=tainted,
                    quoted=quoted,
                    approved_plan=approved_plan,
                )
            except Exception:
                log.exception("Chat answer failed (%s)", channel.name)
                # A turn that failed never showed its writes: they must not run on a yes.
                conv.drop_unshown()
                return self._s.fallback_reply
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
            if may_be_silent and says_nothing(reply.text) and not reply.actions:
                conv.messages = before
                return None
            # With nothing to say it still lists what it did or asks: never the marker.
            said_text = "" if says_nothing(reply.text) and reply.actions else reply.text
            text = self._with_waiting(said_text, reply.actions, conv, channel.option_chars).strip()
            self._save(conv)
        await self.share_files(conv, reply.actions)
        titles = [a["title"] for a in reply.actions if a.get("type") == "reminder"]
        if titles:
            note = self._s.chat_reminders_not_created.format(titles=", ".join(titles))
            text = f"{text}\n\n{note}".strip()
        return text

    async def request(
        self,
        text: str,
        context: str | None = None,
        files: list[Attachment] | None = None,
        *,
        scheduled: bool = False,
    ) -> str:
        """A request that came from elsewhere (a voice shortcut, a scheduler's briefing),
        answered in the chat so the user can go on there. The reply is returned too: a
        chat that can't be reached must not lose it."""
        reply = await self.ask(text, files, context=context, scheduled=scheduled) or ""
        try:
            await self.channel.send(reply)
            await self.after_reply()
        except httpx.HTTPError as exc:
            log.warning("Could not send the reply in the chat (%s): %s", self.name, exc)
        return reply

    async def share_files(self, conv: Conversation, actions: list[dict]) -> None:
        """Send the files the agent made (a PDF, a chart) in the chat."""
        for action in (a for a in actions if a.get("type") == "share_file"):
            file = conv.files.get(action.get("name", ""))
            if file is None:
                continue
            try:
                await self.channel.send_file(file)
            except httpx.HTTPError as exc:
                log.warning("Could not send %s in the chat: %s", file.filename, exc)

    async def run_plans(self) -> None:
        """Each approved plan runs as a request of its own: clean, so it can act in a row,
        and carrying the plan, so the writes it spells out run without another go. It is
        still not the user's own message: what only they may ask for (deleting...) waits
        for them."""
        conv = self.conversation()
        while conv.plans:
            plan = plain(conv.plans.pop(0))
            reply = await self.ask(f"{PLAN_TAG} {plan}]", scheduled=True, approved_plan=plan)
            await self.channel.send(reply or "")
            await self.send_choices()

    # --- what waits for the user -------------------------------------------------------

    async def send_choices(self) -> None:
        """Choices for the writes just listed and the questions just asked. Ticking does
        nothing until the user also ticks the send option: their signal they are done."""
        s, channel = self._s, self.channel
        conv = self.conversation()
        waiting = [p for p in conv.open_proposals() if p.shown and not p.poll_id]
        # Two of a choice's options are the code's own: send and cancel.
        size = max(1, channel.max_options - 2)
        for start in range(0, len(waiting), size):
            batch = waiting[start : start + size]
            options = choice_options(
                [(p.number, p.summary) for p in batch], channel.option_chars, self._language
            )
            try:
                choice_id = await channel.send_choices(
                    s.poll_name, [*options, s.poll_validate, s.poll_cancel]
                )
            except httpx.HTTPError as exc:
                log.warning("Could not send the choice: %s", exc)
                # The message left these to the choice: a yes must never run unseen writes.
                try:
                    await channel.send(
                        s.waiting_header
                        + "".join(f"\n{p.number}. {p.summary}" for p in waiting[start:])
                    )
                except httpx.HTTPError as again:
                    log.warning("Could not list what waits either: %s", again)
                return
            for proposal, option in zip(batch, options, strict=True):
                proposal.poll_id, proposal.option = choice_id or "", option
        for question in (q for q in conv.questions if not q["poll_id"]):
            options = question["options"]
            if _multiple(question):
                options = [*options, s.poll_validate]
            try:
                sent = await channel.send_choices(
                    question["question"], options, _multiple(question)
                )
            except httpx.HTTPError as exc:
                log.warning("Could not send the question: %s", exc)
                return
            question["poll_id"] = sent or ""
        self._save(conv)

    async def _answer_question_choice(self, choice_id: str, selected: list[str]) -> bool:
        """A question the agent asked as a choice: once sent (or, for a single choice,
        once the tap has stood for the grace period, during which it can be changed or
        taken back), the options picked are the user's answer. A vote changed after the
        answer reaches the agent as a new message."""
        conv = self.conversation()
        question = next((q for q in conv.questions if same_message(q["poll_id"], choice_id)), None)
        if question is None:
            return await self._answer_changed(choice_id, selected)
        if _multiple(question):
            if self._s.poll_validate in selected:
                await self._answer_question(question, choice_id, selected)
            return True
        key = _key(choice_id)
        if waiting := self.pending_votes.pop(key, None):
            waiting.cancel()
        if not selected:
            return True
        if not self._grace:
            await self._answer_question(question, choice_id, selected)
            return True

        async def after_grace() -> None:
            await asyncio.sleep(self._grace)
            self.pending_votes.pop(key, None)
            await self._answer_question(question, choice_id, selected)

        self.pending_votes[key] = asyncio.create_task(after_grace())
        return True

    async def _answer_question(self, question: dict, choice_id: str, selected: list[str]) -> None:
        """The option the user ticks is their own answer, as if they had typed it, even
        when the agent wrote the question after reading an email or a page: they saw every
        word of it and chose. Nothing of what was read comes with it (that turn kept only
        its reply); what the answer's turn reads next taints it as usual."""
        conv = self.conversation()
        if question in conv.questions:
            conv.questions.remove(question)
        self.answered[_key(choice_id)] = (question, selected)
        while len(self.answered) > MAX_OPEN_QUESTIONS:
            self.answered.pop(next(iter(self.answered)))
        choices = [plain(o) for o in question["options"] if o in selected]
        text = (
            "[The user answered your poll, quoted as data. Question: "
            f"{plain(question['question'])} | Ticked: {' ; '.join(choices) or 'nothing'}]"
        )
        await self._reply_under(choice_id, text)

    async def _answer_changed(self, choice_id: str, selected: list[str]) -> bool:
        """The user moves their vote on a question already answered: the agent is told, as
        the user's own correction. It can adjust what comes next, not undo what ran."""
        found = self.answered.get(_key(choice_id))
        if found is None:
            return False
        question, before = found
        if not selected or set(selected) == set(before):
            return True
        self.answered[_key(choice_id)] = (question, selected)
        choices = [plain(o) for o in question["options"] if o in selected]
        text = (
            "[The user changed their answer to your poll, quoted as data. Question: "
            f"{plain(question['question'])} | Now ticked: {' ; '.join(choices) or 'nothing'}]"
        )
        await self._reply_under(choice_id, text)
        return True

    async def _reply_under(self, choice_id: str, text: str) -> None:
        await self.channel.send(await self.ask(text) or "", reply_to=choice_id)
        await self.channel.mark(choice_id, DONE)
        await self.after_reply()

    def _noted(self, result: object) -> str:
        """What a write run on the user's go says beside it (nothing to do, a read-back)."""
        note = result.get("note") if isinstance(result, dict) else None
        return self._s.go_note.format(note=plain(str(note))) if note else ""

    async def resume_after_go(self, result: dict, summaries: dict[int, str], *, read: bool) -> None:
        """The user's go is their answer; the agent picks up where it stopped. What the
        approved actions read (a page, a search) comes with it, so that run is tainted."""
        if not (result["done"] or result["failed"]):
            return
        reply = await self.ask(resume_text(result, summaries), tainted=read, may_be_silent=True)
        if reply:
            await self.channel.send(reply)
        await self.send_choices()


def _key(choice_id: str) -> str:
    return choice_id.rsplit("_", 1)[-1]


def resume_text(result: dict, summaries: dict[int, str]) -> str:
    """What the agent gets once the user's go ran the writes it waited for: the request
    they came from goes on by itself, without the user writing it again."""
    done = [
        {"action": plain(summaries.get(d["number"], "")), "result": d["result"]}
        for d in result["done"]
    ]
    failed = [
        {"action": plain(summaries.get(f["number"], "")), "error": f["error"]}
        for f in result["failed"]
    ]
    results = json.dumps({"done": done, "failed": failed}, ensure_ascii=False, default=str)
    return (
        f"{RESUME_TAG} what they approved ran; its results, quoted as data: "
        f"{results[:MAX_RESUME_CHARS]}]\n\nPick up the request these actions came from "
        "where you stopped and do the steps left, in a row, without asking again for what "
        "the user just approved. If nothing is left to do, answer exactly "
        f"{SILENT_REPLY}."
    )


class ChatReports:
    """What the agent says by itself (a task's report, the briefing), sent in a chat: a
    ``deliver.ReportChannel``. The user can answer there, so a task runs in the chat's
    conversation and what it parks for their go is offered as a choice under the report."""

    display = "chat"
    reaches = "a message in the chat they talk to you in"

    def __init__(self, session: ChatSession) -> None:
        self._session = session
        self.name = session.name
        self.option_chars = session.channel.option_chars

    @property
    def conversation_id(self) -> str:
        return self._session.conversation().id

    async def send(self, title: str, text: str) -> None:
        try:
            await self._session.channel.send(f"⏰ {title}\n\n{text}".strip())
        except httpx.HTTPError as exc:
            raise DeliveryError(str(exc)) from exc
        try:
            await self._session.after_reply()
        except httpx.HTTPError as exc:
            # The report went out; what waits can still be approved in writing.
            log.warning("Could not send the choices of a report (%s): %s", self.name, exc)

    async def share_files(self, conv: Conversation, actions: list[dict]) -> None:
        await self._session.share_files(conv, actions)
