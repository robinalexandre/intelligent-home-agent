"""The agent loop: a language model + tools, one conversation turn at a time.

The model is reached through :class:`intelligent_home_agent.llm.LLMClient`, so the loop
never sees a vendor SDK: requests and answers are in the Messages format, as dict blocks.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from intelligent_home_agent.conversations import Attachment, Conversation
from intelligent_home_agent.engine import EngineTurn, FileInMessage, SubscriptionUnavailable
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.journal import Journal, Turn
from intelligent_home_agent.llm import LLMClient
from intelligent_home_agent.office import is_iwork, iwork_preview, office_text
from intelligent_home_agent.prompts import PromptConfig, build_system_prompt
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.base import WithImage, bare_name
from intelligent_home_agent.tools.long_results import PART_CHARS, LongResult, first_part, keep_long

log = logging.getLogger(__name__)

OUT_OF_ROUNDS = (
    "No tool rounds are left for this request: answer now with what you found, and say "
    "plainly what is still missing or left to do."
)
# The latest turns stay whole (follow-ups often need their details); older ones are
# compacted to the question, the answer and the tools used.
KEEP_FULL_TURNS = 2
MAX_TRACE_CHARS = 1_500
# Tool arguments worth remembering to find something again; never free text.
TRACED_ARGS = frozenset({"id", "account", "path", "url", "name", "file_name", "repo"})
CUT_OFF = (
    "Your last reply was too long and got cut off before it ended: do it again, shorter "
    "(a short email body, fewer tool calls at once)."
)
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT_FILE_CHARS = 50_000

# How a task the user left starts, and how the last report a repeating one carries does.
TASK_TAG = "[Scheduled task the user left you:"
REPEATING_TAG = "[Repeating task"
# A quote, a reaction or a poll answer ends with the agent's words, before the user's own.
QUOTE_END = "»]\n\n"
QUOTE_START = "quoted as data: «"
# Prompt caching: the tools, the system prompt and the conversation so far are the same
# from one call to the next (a turn makes one call per round of tools), so each request
# marks them and only what is new is paid at the full price.
CACHE = {"cache_control": {"type": "ephemeral"}}


class Engine(Protocol):
    """A way to run a whole turn elsewhere than the API (intelligent_home_agent.engine):
    it calls the agent's tools back through ``call`` and returns the turn as API messages."""

    @property
    def available(self) -> bool: ...

    async def run_turn(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: ToolRegistry,
        call: Callable[[str, dict[str, Any]], Awaitable[tuple[Any, bool]]],
        *,
        max_turns: int,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineTurn: ...


@dataclass
class AgentReply:
    text: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    # A repeating task's run was refused a tool its clean runs would get.
    wants_clean_runs: bool = False


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        prompt: PromptConfig,
        *,
        model: str,
        max_tokens: int,
        max_tool_iterations: int,
        max_history_messages: int,
        max_chat_tool_iterations: int | None = None,
        max_chat_tokens: int | None = None,
        memory: Callable[[], Awaitable[str]] | None = None,
        journal: Journal | None = None,
        engine: Engine | None = None,
        engine_displays: frozenset[str] = frozenset({"chat"}),
        api_fallback: bool = True,
    ) -> None:
        self._client = llm
        # Runs the turns when it can (the user's Claude subscription); the API takes the
        # others. Only for these displays: the engine starts a process per turn, a few
        # seconds a chat can wait for and a voice hardly can. Without an API to fall back
        # on (no key), the engine answers every display and says so when it can't.
        self._engine = engine
        self._engine_displays = engine_displays
        self._api_fallback = api_fallback
        self._journal = journal
        self._tools = tools
        self._prompt = prompt
        # The prompt only describes the tools that are actually registered.
        self._prompt.tools = tools.names()
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_tool_iterations
        self._max_chat_iterations = max_chat_tool_iterations or max_tool_iterations
        self._max_chat_tokens = max_chat_tokens or max_tokens
        # What the agent remembers about the user, added to the prompt at each turn.
        self._memory = memory
        self._max_history = max_history_messages

    @property
    def engine(self) -> Engine | None:
        return self._engine

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    async def ask(
        self,
        conv: Conversation,
        text: str,
        context: str | None = None,
        *,
        display: str = "voice",
        files: list[Attachment] | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
        scheduled: bool = False,
        tainted: bool = False,
        quoted: bool | str = False,
        approved_plan: str = "",
        answerable: bool = True,
    ) -> AgentReply:
        """Run one user turn to completion and return the reply.

        ``context`` is extra data the client sends along (e.g. today's reminders from the
        phone); it is passed to the model as data, not as part of the request.
        ``display`` is where the reply ends up: spoken ("voice"), shown as a notification,
        or sent in a chat the user can answer.
        ``files`` are read in this turn only; afterwards the conversation keeps them by
        name, for the tools that take a file, and a note replaces their content in the
        history.
        ``progress`` receives what the model says before each round of tools, so a chat
        can show what it is doing while it works.
        ``scheduled`` marks a task the user left earlier, not a message they just wrote.
        ``tainted`` starts the turn as if it had read others' content: no writes at all.
        ``quoted``: the message quotes an earlier one; writes wait for the user's go. "own"
        says it quotes one of this agent's replies, which still counts as the user's own
        request for acting on a site.
        ``approved_plan``: the plan the agent proposed and the user approved as shown, which
        the text carries: the writes it spells out run without another go while the turn
        reads only web pages.
        ``answerable``: False when the user can't answer this reply (a scheduler's briefing
        in a conversation of its own): what would wait for their go is refused instead.
        """
        conv.turn += 1
        turn = Turn(display, text, scheduled, quoted, [f.filename for f in files or []])
        started = time.monotonic()
        if progress is not None:
            send_progress = progress

            async def progress(update: str) -> None:
                turn.progress += 1
                await send_progress(update)

        own = own_words(text, approved_plan)
        if quoted == "own" and conv.said_cleanly(quoted_words(text)):
            # One of its replies to a request that read nothing of others': the quote holds
            # nothing the history doesn't, so the message is the user's own request.
            quoted = False
        if context:
            text = f"{text}\n\n<phone_context>\n{context}\n</phone_context>"
        user_message: dict[str, Any] = {"role": "user", "content": text}
        if files:
            for file in files:
                conv.add_file(file)
            names = ", ".join(f.filename for f in files)
            user_message["content"] = [*(b for f in files for b in file_blocks(f)), _text(text)]
            remembered = {"role": "user", "content": f"{text}\n\n[Files sent and read: {names}]"}
        start = len(conv.messages)
        conv.messages.append(user_message)
        # A file can be written by anyone (an invoice, a letter): treat it like an email.
        ctx = ToolContext(
            conversation=conv,
            tainted=bool(files) or tainted,
            scheduled=scheduled,
            quoted=quoted,
            approved_plan=approved_plan,
            repeating=text.startswith(TASK_TAG) and f"\n\n{REPEATING_TAG}" in text,
            answerable=answerable,
        )
        ctx.name_hosts(own)
        # The agent's earlier words and results must not leave in what it writes now; what
        # the user's own words spell out may.
        ctx.leaks.add_messages(conv.messages[:start])
        ctx.leaks.allow(own)
        try:
            reply = await self._run_turn(conv, ctx, display, progress) or self._fallback()
        except BaseException as exc:
            self._log_turn(
                turn,
                conv.messages[start:],
                "",
                ctx.tainted,
                f"error: {type(exc).__name__}",
                started,
            )
            # Never keep a half-finished turn: it may hold private content or dangling
            # tool results that would break the next request.
            del conv.messages[start:]
            raise
        finally:
            await ctx.finish()
        self._log_turn(
            turn,
            conv.messages[start:],
            reply,
            ctx.tainted,
            _outcome(conv.messages[start:], reply, self._fallback()),
            started,
        )

        if not (ctx.tainted or ctx.quoted):
            conv.note_clean_reply(reply)
        if ctx.tainted:
            # Emails and events stay out of memory once answered: only the question, the
            # reply and which tools were used are kept, so text injected in them can't
            # steer later requests.
            conv.messages[start:] = [
                remembered if files else user_message,
                _assistant(reply + _tool_trace(conv.messages[start:])),
            ]
        compact_old_turns(conv.messages)
        conv.trim(self._max_history)
        return AgentReply(reply, ctx.actions, bool(ctx.wants_clean_runs))

    async def _run_turn(
        self,
        conv: Conversation,
        ctx: ToolContext,
        display: str,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        memory = await self._memory() if self._memory else ""
        ctx.leaks.add(memory)
        system = build_system_prompt(self._prompt, display=display, memory=memory)
        # A chat can wait for a long search and read a long answer; a voice can't.
        chat = display == "chat"
        rounds = self._max_chat_iterations if chat else self._max_iterations
        tokens = self._max_chat_tokens if chat else self._max_tokens
        if self._engine is not None and (
            display in self._engine_displays or not self._api_fallback
        ):
            reply = await self._run_on_engine(conv, ctx, system, rounds, progress)
            if reply is not None:
                return reply
        for _ in range(rounds):
            response = await self._client.create(
                model=self._model,
                max_tokens=tokens,
                system=cached_system(system),
                tools=cached_tools(self._tools.definitions()),
                messages=cached_messages(conv.messages),
            )
            content = response.content
            if response.stop_reason == "max_tokens":
                # Cut off, maybe in the middle of a tool call: drop it and try shorter.
                log.warning("Reply cut off at max_tokens in conversation %s", conv.id)
                _add_note(conv.messages[-1], CUT_OFF)
                continue
            conv.messages.append({"role": "assistant", "content": content})
            if response.stop_reason != "tool_use":
                text = _text_of(content)
                if not text:
                    log.warning(
                        "Empty reply (%s) in conversation %s", response.stop_reason, conv.id
                    )
                return text
            if progress and (update := _text_of(content)):
                try:
                    await progress(update)
                except Exception:
                    log.warning("Could not send a progress update", exc_info=True)

            calls = [block for block in content if block["type"] == "tool_use"]
            # Calls in one batch were all decided before any of them ran, so the taint only
            # applies from the next batch on.
            ctx.begin_batch()
            results = await asyncio.gather(*(self._run_tool(block, ctx) for block in calls))
            if any(self._tools.taints(call["name"]) for call in calls):
                ctx.tainted = True
            # A search refused before it ran (a query that would leak, a second one) read
            # nothing: the corrected query may still go out.
            if any(
                self._tools.reads_web(call["name"]) and not result.get("is_error")
                for call, result in zip(calls, results, strict=True)
            ):
                ctx.web_read = True
            conv.messages.append({"role": "user", "content": list(results)})

        log.warning("Tool loop limit reached in conversation %s", conv.id)
        # Out of rounds: answer with what was found rather than a bare error. This also
        # keeps the history from ending with dangling tool results.
        _add_note(conv.messages[-1], OUT_OF_ROUNDS)
        response = await self._client.create(
            model=self._model,
            max_tokens=tokens,
            system=cached_system(system),
            tools=cached_tools(self._tools.definitions()),
            tool_choice={"type": "none"},
            messages=cached_messages(conv.messages),
        )
        content = response.content
        text = _text_of(content)
        conv.messages.append({"role": "assistant", "content": content or [_text("(stopped)")]})
        return text or None

    async def _run_on_engine(
        self,
        conv: Conversation,
        ctx: ToolContext,
        system: str,
        rounds: int,
        progress: Callable[[str], Awaitable[None]] | None,
    ) -> str | None:
        """The turn on the engine; None when it could not take it and nothing ran yet, so
        the API does it instead. Once a tool ran, the turn is not done twice: it fails."""
        called = False

        async def call(name: str, args: dict[str, Any]) -> tuple[Any, bool]:
            nonlocal called
            called = True
            # Here the tools run one at a time: the taint applies from the next call on.
            ctx.begin_batch()
            result = await self._run_tool({"id": "", "name": name, "input": args}, ctx)
            if self._tools.taints(name):
                ctx.tainted = True
            if self._tools.reads_web(name) and not result.get("is_error"):
                ctx.web_read = True
            return result["content"], bool(result.get("is_error"))

        words = strings(self._prompt.language)
        try:
            if not self._engine.available:
                raise SubscriptionUnavailable("the engine is paused")
            turn = await self._engine.run_turn(
                system, conv.messages, self._tools, call, max_turns=rounds, progress=progress
            )
        except SubscriptionUnavailable as exc:
            if called:
                raise
            if self._api_fallback:
                log.warning("Engine unavailable, the API takes this turn: %s", exc)
                return None
            # No API key: nothing else can answer, so the user hears why.
            log.warning("Engine unavailable and no API key to take this turn: %s", exc)
            file = isinstance(exc, FileInMessage)
            text = words.subscription_no_file if file else words.subscription_unavailable
            conv.messages.append(_assistant(text))
            return text
        conv.messages.extend(turn.messages)
        if turn.out_of_rounds:
            # Like the API's own loop: the tools that ran stay done, the reply says so.
            log.warning("Engine tool loop limit reached in conversation %s", conv.id)
            text = f"{turn.text}\n\n{words.stopped_reply}" if turn.text else words.stopped_reply
            conv.messages.append(_assistant(text))
            return text
        if not turn.text:
            log.warning("Empty reply from the engine in conversation %s", conv.id)
        return turn.text

    def _fallback(self) -> str:
        return strings(self._prompt.language).fallback_reply

    def _log_turn(
        self,
        turn: Turn,
        messages: list[dict[str, Any]],
        reply: str,
        tainted: bool,
        outcome: str,
        started: float,
    ) -> None:
        if self._journal is None:
            return
        entry = turn.record(
            messages,
            reply=reply,
            tainted=tainted,
            outcome=outcome,
            seconds=time.monotonic() - started,
            at=self._journal.now(),
        )
        self._journal.add(entry)

    def _kept_apart(self, name: str, content: str, ctx: ToolContext) -> str:
        """A result too long for one read: its first part now, the others on demand,
        each carrying what the whole did for the request (tools/long_results.py)."""
        tool = self._tools.get(name)
        kept = LongResult(
            tool=name,
            text=content,
            private=bool(tool and tool.taints),
            web=bool(tool and tool.reads_web),
            hosts=frozenset(ctx.page_hosts) if tool and tool.reads_pages else frozenset(),
        )
        return first_part(keep_long(ctx.conversation, kept), kept)

    async def _run_tool(self, block: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name, args = block["name"], block.get("input") or {}
        log.info("tool call %s %s", name, json.dumps(args, ensure_ascii=False))
        try:
            result = await self._tools.run(name, args, ctx)
            if isinstance(result, Attachment):
                # A file read by a tool: the model reads it as it would one the user sent.
                content: Any = [*file_blocks(result), _text(f"Kept as {result.filename}.")]
            elif isinstance(result, WithImage):
                text = _json(result.data)
                ctx.leaks.add(text)
                content = [_image(result.mimetype, result.image), _text(text[:PART_CHARS])]
            else:
                content = result if isinstance(result, str) else _json(result)
                ctx.leaks.add(content)
                if len(content) > PART_CHARS and bare_name(name) != "read_result_part":
                    content = self._kept_apart(name, content, ctx)
            return {"type": "tool_result", "tool_use_id": block["id"], "content": content}
        except ToolError as exc:
            log.info("tool %s refused: %s", name, exc)
            return _error_result(block["id"], str(exc))
        except Exception:
            log.exception("tool %s crashed", name)
            return _error_result(block["id"], "Internal error while running this tool.")


def own_words(text: str, approved_plan: str = "") -> str:
    """What the user wrote themselves in this request: a task's text without its last
    report, the words after a quote, never an approved plan the agent wrote."""
    if approved_plan:
        return ""
    if text.startswith(TASK_TAG):
        return text.partition(f"\n\n{REPEATING_TAG}")[0]
    if text.startswith("["):
        return text.rpartition(QUOTE_END)[2] if QUOTE_END in text else ""
    return text


def quoted_words(text: str) -> str:
    """The earlier message a quote or a reaction shows, as the agent was given it."""
    if not text.startswith("[") or QUOTE_END not in text:
        return ""
    return text.rpartition(QUOTE_END)[0].partition(QUOTE_START)[2]


def _json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def cached_system(system: str) -> list[dict[str, Any]]:
    return [{**_text(system), **CACHE}]


def cached_tools(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not definitions:
        return definitions
    return [*definitions[:-1], {**definitions[-1], **CACHE}]


def cached_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages with the last block of the last one marked, in a copy: the history
    itself keeps no marker, so the next request moves it to its own last message."""
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    blocks = [_text(content)] if isinstance(content, str) else list(content)
    blocks[-1] = {**blocks[-1], **CACHE}
    return [*messages[:-1], {**last, "content": blocks}]


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [_text(text)]}


def _is_turn_start(message: dict[str, Any]) -> bool:
    if message["role"] != "user":
        return False
    content = message["content"]
    return isinstance(content, str) or not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _tool_trace(messages: list[dict[str, Any]]) -> str:
    """Which tools a turn used and with what, so a later turn can find things again."""
    calls = []
    for message in messages:
        if message["role"] == "assistant" and isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "tool_use":
                    # References only: free text (a draft's body, a query) can carry words
                    # injected by what the turn read, and must not come back as the agent's.
                    args = ", ".join(
                        f"{k}={' '.join(str(v).split())[:120]}"
                        for k, v in (block.get("input") or {}).items()
                        if k in TRACED_ARGS
                    )
                    calls.append(f"{block['name']}({args})")
    if not calls:
        return ""
    return f"\n\n[Tools used: {'; '.join(calls)}]"[:MAX_TRACE_CHARS]


def compact_old_turns(messages: list[dict[str, Any]]) -> None:
    """Keep the last turns whole; older ones become question, answer and tools used."""
    starts = [i for i, m in enumerate(messages) if _is_turn_start(m)]
    for index in range(len(starts) - KEEP_FULL_TURNS - 1, -1, -1):
        start = starts[index]
        end = starts[index + 1]
        turn = messages[start:end]
        if len(turn) <= 2:
            continue  # already compact
        replies = [m for m in turn if m["role"] == "assistant"]
        answer = _text_of(replies[-1]["content"]) if replies else ""
        question = turn[0]
        if not isinstance(question["content"], str):
            question = {"role": "user", "content": _text_of(question["content"])}
        messages[start:end] = [question, _assistant((answer or "(no answer)") + _tool_trace(turn))]


def _outcome(messages: list[dict[str, Any]], reply: str, fallback: str) -> str:
    if reply == fallback:
        return "fallback"
    notes = {
        block.get("text")
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict)
    }
    return "out_of_rounds" if OUT_OF_ROUNDS in notes else "ok"


def _add_note(message: dict[str, Any], note: str) -> None:
    """Add a note for the model to the last user message (text or content blocks)."""
    if isinstance(message["content"], str):
        message["content"] = [_text(message["content"])]
    message["content"].append(_text(note))


def file_blocks(file: Attachment) -> list[dict[str, Any]]:
    """Content blocks the model can read: PDFs and images natively (a provider that
    can't is told so by its client, see llm.to_chat_messages), text and office files as
    text, Apple's iWork files as their first page's preview."""
    if file.mimetype == "application/pdf" and len(file.data) <= MAX_PDF_BYTES:
        source = {"type": "base64", "media_type": file.mimetype, "data": _b64(file.data)}
        return [{"type": "document", "source": source, "title": file.filename}]
    if file.mimetype in IMAGE_TYPES and len(file.data) <= MAX_IMAGE_BYTES:
        return [_image(file.mimetype, file.data)]
    if file.mimetype.startswith("text/") or file.mimetype == "application/rtf":
        return [_file_text(file.filename, file.data.decode("utf-8", errors="replace"))]
    try:
        content = office_text(file)
        preview = iwork_preview(file) if content is None and is_iwork(file) else None
    except Exception as exc:
        log.warning("Could not read %s: %s", file.filename, exc)
        content = preview = None
    if content is not None:
        return [_file_text(file.filename, content)]
    if preview is not None and len(preview) <= MAX_IMAGE_BYTES:
        return [
            _image("image/jpeg", preview),
            _text(
                f"[{file.filename}: only its first page's preview can be seen; for the whole "
                "file, ask the user for a PDF export.]"
            ),
        ]
    return [
        _text(f"[{file.filename}: too big or in a format that can't be read; it can be attached.]")
    ]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _image(media_type: str, data: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": _b64(data)},
    }


def _file_text(name: str, content: str) -> dict[str, Any]:
    return _text(f'<file name="{name}">\n{content[:MAX_TEXT_FILE_CHARS]}\n</file>')


def _error_result(tool_use_id: str, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}


def _text_of(content: list[dict[str, Any]]) -> str:
    return " ".join(b["text"].strip() for b in content if b["type"] == "text").strip()
