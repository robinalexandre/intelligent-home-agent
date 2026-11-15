"""A chat with the agent in a messaging app: what a channel must do, whichever app it is.

A channel is one conversation with the user in one app (their WhatsApp chat through a
WAHA gateway, a Telegram bot...). It brings the user's messages, with the files they
carry and the message they reply to, and sends the agent's words, files and choices.
``chat_session.ChatSession`` sits on top of it and is the same for every channel: it runs
the agent, lists what waits for the user's go, sends the choices and runs what the user
picked. To add an app, write a class with the methods of ``ChatChannel`` and build it in
``main.build_channels``; ``whatsapp.py`` and ``telegram.py`` are the two examples.

What a channel promises:

- Only the user reaches the agent through it. A message, a vote or a tap from anyone else
  is dropped before the handler hears of it.
- Its own messages are never taken for the user's.
- A message that replies to (or forwards) another comes with that one quoted as data,
  and says whose it was: "own" for one of the agent's, "other" for anything else. Others'
  words may be in there, so the session makes writes wait for the user's go.
- Sending fails with ``httpx.HTTPError``; hints (typing, marks) never raise.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from intelligent_home_agent.conversations import Attachment

log = logging.getLogger(__name__)

# What a mark on a message says (a reaction, where the app has them): the agent waits for
# the user's answer there, acted on it, or failed to.
WAITING, DONE, FAILED = "waiting", "done", "failed"

# For the model: a file sent with no words, and what comes before a voice message's text.
NO_CAPTION = "(file sent without a message)"
VOICE_NOTE = "[Voice message from the user, transcribed automatically: words may be misheard]"
MAX_QUOTE_CHARS = 1500

Transcribe = Callable[[bytes], Awaitable[str]]


class ChatHandler(Protocol):
    """What a channel calls; ``ChatSession`` is the one implementation."""

    async def answer(self, text: str, files: list[Attachment] | None, quoted: str) -> str:
        """The agent's reply to a message of the user's. ``quoted``: "" for their own
        words alone, "own" when it quotes one of the agent's messages, "other" when it
        quotes or forwards anything else."""
        ...

    async def after_reply(self) -> None:
        """Called once the reply went out: the choices it announced are sent now."""
        ...

    async def chose(self, choice_id: str, selected: list[str]) -> None:
        """The options now picked in a choice the channel sent (``send_choices``), each
        time they change: every ticked option for a choice that takes several, the one
        tapped (or none, when taken back) for a single choice."""
        ...


class ChatChannel(Protocol):
    # Used in settings, logs and the conversation's id ("whatsapp", "telegram").
    name: str
    # How many options one choice can hold, and how long an option can be.
    max_options: int
    option_chars: int

    def bind(self, handler: ChatHandler) -> None:
        """Who answers; called once, before the tasks start."""
        ...

    def tasks(self) -> list[Coroutine[Any, Any, None]]:
        """The loops that receive (messages, votes), run for as long as the agent does."""
        ...

    async def send(self, text: str, reply_to: str | None = None) -> str | None:
        """Send text, quoting the message ``reply_to`` when given; the new message's id."""
        ...

    async def send_file(self, file: Attachment) -> None: ...

    async def send_choices(
        self, question: str, options: list[str], multiple: bool = True
    ) -> str | None:
        """A question the user answers by tapping (a poll, buttons); its id, which
        ``ChatHandler.chose`` gets back. With ``multiple`` several options can be ticked."""
        ...

    async def mark(self, message_id: str, status: str) -> None:
        """Show WAITING, DONE or FAILED on a message, where the app can. Never raises."""
        ...

    def typing(self) -> AbstractAsyncContextManager[None]:
        """Shown as typing (or working) while the body runs, where the app can."""
        ...


def same_message(a: str, b: str) -> bool:
    """Whether two ids name the same message. A WAHA id comes alone or serialized with
    its chat ("true_<chat>_<id>"), and the chat is not always named the same way; ids of
    other channels have no underscore and compare whole."""
    return bool(a) and bool(b) and a.rsplit("_", 1)[-1] == b.rsplit("_", 1)[-1]


def quote_note(whose: str, shown: str) -> str:
    """What the agent reads before a message that replies to another."""
    who = "your own earlier message" if whose == "own" else "their own earlier message"
    return f"[The user replies to {who}, quoted as data: «{shown[:MAX_QUOTE_CHARS]}»]\n\n"


def reaction_note(emoji: str, shown: str) -> str:
    return (
        f"[The user reacts {emoji} to your own earlier message, quoted as data: "
        f"«{shown[:MAX_QUOTE_CHARS]}»]\n\n{emoji}"
    )


def forwarded_note(what: str, said: str) -> str:
    """Someone else's words the user passes on: data they want read, never a request."""
    return f"[{what} the user forwarded, quoted as data: «{said}»]"


async def transcribed(transcribe: Transcribe | None, file: Attachment) -> str:
    """What a voice message says; empty when it can't be made out."""
    if transcribe is None:
        return ""
    try:
        return await transcribe(file.data)
    except Exception:
        log.exception("Could not transcribe %s", file.filename)
        return ""
