"""Telegram chat with the agent, through a bot of the user's own (the Bot API).

The user creates a bot with @BotFather and gives its token; the agent asks Telegram for
what was sent to the bot (long polling on ``getUpdates``: no public address, no webhook)
and answers with ``sendMessage``. Nothing else is needed: no gateway to run, and the bot's
messages notify like anyone's.

Anyone can find a bot and write to it, so only the private chats listed in
TELEGRAM_CHAT_IDS reach the agent: a message, a tap on a button or a reaction from anyone
else, or from a group, is dropped before anything reads it. Each listed chat is a
conversation of its own.

Choices (what waits for the user's go, a question with a few answers) are buttons under a
message. Which ones are ticked is kept in the buttons themselves, so they still work
after the agent restarts. A message that replies to an earlier one, or that the user
forwards, comes with it quoted as data, as in the other channels. Files are fetched with
``getFile`` (20 MB at most, the Bot API's limit); a voice message is transcribed on the
server when transcription is set up.

Written from the Bot API's documentation (core.telegram.org/bots/api) and tested against
a fake of it: see docs/chat.md for what that means.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx

from intelligent_home_agent.chat import (
    DONE,
    FAILED,
    MAX_QUOTE_CHARS,
    NO_CAPTION,
    VOICE_NOTE,
    WAITING,
    ChatHandler,
    Transcribe,
    forwarded_note,
    quote_note,
    reaction_note,
    transcribed,
)
from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
# What a bot may download (the Bot API's limit).
MAX_FILE_BYTES = 20 * 1024 * 1024
# A message takes 4096 UTF-16 units; kept under it, as a character can take two.
MAX_MESSAGE_CHARS = 3500
# How long Telegram holds a getUpdates call open when nothing comes.
POLL_TIMEOUT_SECONDS = 25
# The least time between two calls that brought nothing.
MIN_POLL_SECONDS = 1.0
# Telegram drops "typing" after five seconds: it is renewed before that.
TYPING_REFRESH_SECONDS = 4
STOPPED_NOTICE_SECONDS = 5
MAX_KNOWN_MESSAGES = 200
UPDATES = ["message", "callback_query", "message_reaction"]
# Reactions a bot may put (Telegram's fixed list has no check mark nor cross).
MARKS = {WAITING: "👀", DONE: "👍", FAILED: "🤷"}
# Shown before a ticked button's text; an option never starts with it.
TICK = "☑ "
# Buttons, one per row: kept as for a poll, so every channel lists the same way.
MAX_BUTTONS = 12
# A button shows about this much on a phone: longer options are also written out in the
# message, and a write longer than this is listed in the reply instead.
BUTTON_CHARS = 48
# Files shown in the chat as a picture or a video rather than a document.
MEDIA_METHODS = {
    "image/png": ("sendPhoto", "photo"),
    "image/jpeg": ("sendPhoto", "photo"),
    "video/mp4": ("sendVideo", "video"),
}
_BOLD = re.compile(r"\*([^*\n]+)\*")


class TelegramError(httpx.HTTPError):
    """A call to the Bot API failed. Its message never holds the address called, which
    carries the bot's token."""


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def bold_entities(text: str) -> tuple[str, list[dict[str, Any]]]:
    """The agent writes *bold* for a key word: the text without the asterisks, and where
    the bold words are, as Telegram counts (UTF-16 units). No parse mode is used, so no
    other character of a reply can break the message."""
    plain, entities, last = "", [], 0
    for match in _BOLD.finditer(text):
        plain += text[last : match.start()]
        entities.append({"type": "bold", "offset": _utf16(plain), "length": _utf16(match.group(1))})
        plain += match.group(1)
        last = match.end()
    return plain + text[last:], entities


def chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """A long reply as several messages, cut at a line break when there is one."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*parts, text] if text or not parts else parts


class TelegramApi:
    def __init__(self, http: httpx.AsyncClient, token: str, api_url: str = API_URL) -> None:
        self._http = http
        self._base = f"{api_url.rstrip('/')}/bot{token}"
        self._files = f"{api_url.rstrip('/')}/file/bot{token}"

    async def _post(self, method: str, patience: float | None = None, **kwargs: Any) -> Any:
        """``patience``: how long to wait for the answer, when not the client's default."""
        try:
            resp = await self._http.post(
                f"{self._base}/{method}",
                timeout=patience if patience is not None else httpx.USE_CLIENT_DEFAULT,
                **kwargs,
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
        except httpx.HTTPError as exc:
            # Without the request's address: it holds the token.
            raise TelegramError(f"Telegram {method} failed: {type(exc).__name__}") from None
        if resp.status_code >= 400 or not (isinstance(data, dict) and data.get("ok")):
            why = data.get("description") if isinstance(data, dict) else None
            raise TelegramError(
                f"Telegram {method} refused ({resp.status_code}): {why or 'no reason given'}"
            )
        return data.get("result")

    async def call(self, method: str, patience: float | None = None, **params: Any) -> Any:
        body = {name: value for name, value in params.items() if value is not None}
        return await self._post(method, patience, json=body)

    async def get_updates(self, offset: int | None, hold: int) -> list[dict[str, Any]]:
        """What was sent to the bot since ``offset``; Telegram holds the call open for
        ``hold`` seconds when nothing came."""
        updates = await self.call(
            "getUpdates", hold + 10, offset=offset, timeout=hold, allowed_updates=UPDATES
        )
        return updates if isinstance(updates, list) else []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> str | None:
        """Send text (in several messages when long); the id of the last one sent."""
        message_id = None
        parts = chunks(text)
        for n, part in enumerate(parts):
            plain, entities = bold_entities(part)
            last = n == len(parts) - 1
            sent = await self.call(
                "sendMessage",
                chat_id=chat_id,
                text=plain or "…",
                entities=entities or None,
                # A message quoted that was deleted since must not lose the answer.
                reply_parameters=(
                    {"message_id": int(reply_to), "allow_sending_without_reply": True}
                    if reply_to and n == 0 and reply_to.isdigit()
                    else None
                ),
                reply_markup=reply_markup if last else None,
            )
            message_id = str((sent or {}).get("message_id") or "") or None
        return message_id

    async def send_file(self, chat_id: int, file: Attachment, caption: str) -> str | None:
        """A picture or a video shows as such, anything else as a document; what Telegram
        refuses as a picture (too large, an odd shape) still goes as a document."""
        method, field = MEDIA_METHODS.get(file.mimetype, ("sendDocument", "document"))
        if method != "sendDocument":
            try:
                return await self._upload(method, field, chat_id, file, caption)
            except TelegramError as exc:
                log.info("%s, sent as a document", exc)
        return await self._upload("sendDocument", "document", chat_id, file, caption)

    async def _upload(
        self, method: str, field: str, chat_id: int, file: Attachment, caption: str
    ) -> str | None:
        sent = await self._post(
            method,
            60,
            data={"chat_id": str(chat_id), "caption": caption[:1024]},
            files={field: (file.filename, file.data, file.mimetype)},
        )
        return str((sent or {}).get("message_id") or "") or None

    async def download(self, file_id: str, name: str, mimetype: str) -> Attachment | None:
        """A file sent to the bot, or None when it is too large for a bot to fetch."""
        info = await self.call("getFile", file_id=file_id)
        path = (info or {}).get("file_path")
        if not path or int((info or {}).get("file_size") or 0) > MAX_FILE_BYTES:
            return None
        try:
            resp = await self._http.get(f"{self._files}/{path}", timeout=60)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram file download failed: {type(exc).__name__}") from None
        if len(resp.content) > MAX_FILE_BYTES:
            return None
        return Attachment(name or path.rsplit("/", 1)[-1], mimetype, resp.content)


def _keyboard(options: list[str], multiple: bool, ticked: set[int]) -> dict[str, Any]:
    kind = "m" if multiple else "s"
    return {
        "inline_keyboard": [
            [{"text": (TICK if n in ticked else "") + option, "callback_data": f"{kind}:{n}"}]
            for n, option in enumerate(options)
        ]
    }


def _buttons(message: dict[str, Any]) -> list[str]:
    """The texts of a message's buttons, as they show now (ticks included)."""
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return [str(button.get("text") or "") for row in rows for button in row[:1]]


def _file_of(message: dict[str, Any]) -> tuple[str, str, str] | None:
    """The file a message carries, as (file id, name, type); None when it has none the
    agent can read (a sticker, a location...)."""
    number = message.get("message_id")
    for kind in ("voice", "audio", "document", "video"):
        part = message.get(kind)
        if isinstance(part, dict) and part.get("file_id"):
            fallback = {"voice": "audio/ogg", "audio": "audio/mpeg", "video": "video/mp4"}
            mimetype = str(part.get("mime_type") or fallback.get(kind, "application/octet-stream"))
            name = str(part.get("file_name") or f"{kind}-{number}")
            return str(part["file_id"]), name, mimetype.split(";")[0]
    photos = [p for p in message.get("photo") or [] if isinstance(p, dict) and p.get("file_id")]
    if photos:
        # Telegram gives the same picture in several sizes: the largest one.
        largest = max(photos, key=lambda p: int(p.get("width") or 0) * int(p.get("height") or 0))
        return str(largest["file_id"]), f"photo-{number}.jpg", "image/jpeg"
    return None


class TelegramChannel:
    """The chat with one user of the bot (a ``chat.ChatChannel``)."""

    max_options = MAX_BUTTONS
    option_chars = BUTTON_CHARS

    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        *,
        name: str = "telegram",
        language: str = "en",
        transcribe: Transcribe | None = None,
        typing_seconds: float = TYPING_REFRESH_SECONDS,
    ) -> None:
        self.name = name
        self.chat_id = chat_id
        self._api = api
        self._strings = strings(language)
        self._transcribe = transcribe
        self._typing_seconds = typing_seconds
        self._handler: ChatHandler | None = None
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        # The texts and choices sent, by message id: a reaction on one answers it, a reply
        # to a choice means its question and options.
        self._texts: dict[str, str] = {}
        self._choices: dict[str, str] = {}

    def bind(self, handler: ChatHandler) -> None:
        self._handler = handler

    def tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self.work_messages(), self.work_events()]

    def _remember(self, kept: dict[str, str], message_id: str | None, text: str) -> None:
        if message_id:
            kept[message_id] = text[:MAX_QUOTE_CHARS]
            while len(kept) > MAX_KNOWN_MESSAGES:
                kept.pop(next(iter(kept)))

    async def send(self, text: str, reply_to: str | None = None) -> str | None:
        message_id = await self._api.send_message(self.chat_id, text, reply_to)
        self._remember(self._texts, message_id, text)
        return message_id

    async def send_file(self, file: Attachment) -> None:
        await self._api.send_file(self.chat_id, file, file.filename)

    async def send_choices(
        self, question: str, options: list[str], multiple: bool = True
    ) -> str | None:
        """The question, with one button per option. A tap ticks it (several can be, with
        ``multiple``); an option too long for a button is also written out above them."""
        options = [option.removeprefix(TICK) for option in options]
        text = question
        if any(len(option) > BUTTON_CHARS for option in options):
            text += "\n" + "\n".join(f"- {option}" for option in options)
        message_id = await self._api.send_message(
            self.chat_id, text, reply_markup=_keyboard(options, multiple, set())
        )
        self._remember(self._choices, message_id, f"poll «{question}»: " + " / ".join(options))
        return message_id

    async def mark(self, message_id: str, status: str) -> None:
        """A reaction is a hint for the user: it never gets in the way of the answer."""
        if not message_id.isdigit():
            return
        try:
            await self._api.call(
                "setMessageReaction",
                chat_id=self.chat_id,
                message_id=int(message_id),
                reaction=[{"type": "emoji", "emoji": MARKS[status]}],
            )
        except httpx.HTTPError as exc:
            log.warning("Could not react on Telegram: %s", exc)

    @contextlib.asynccontextmanager
    async def typing(self) -> AsyncIterator[None]:
        """Shown typing while the body runs; Telegram drops it by itself once it stops
        being renewed, so a stopped agent is seen as such."""
        keep = asyncio.create_task(self._keep_typing())
        try:
            yield
        finally:
            keep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keep

    async def _keep_typing(self) -> None:
        while True:
            try:
                await self._api.call("sendChatAction", chat_id=self.chat_id, action="typing")
            except httpx.HTTPError as exc:
                log.warning("Could not show typing: %s", exc)
            await asyncio.sleep(self._typing_seconds)

    # --- what the user sends -----------------------------------------------------------

    async def work_messages(self) -> None:
        """The user's messages, answered one at a time, in the order they came."""
        while True:
            message = await self.messages.get()
            try:
                await self.handle(message)
            except Exception:
                log.exception("Telegram answer failed")

    async def work_events(self) -> None:
        """Taps and reactions, apart from the messages: a tap is taken while the agent
        still works on a message."""
        while True:
            kind, payload = await self.events.get()
            try:
                if kind == "callback":
                    await self.tapped(payload)
                else:
                    await self.reacted(payload)
            except Exception:
                log.exception("Telegram %s failed", kind)

    async def handle(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("message_id") or "")
        try:
            async with self.typing():
                await self._answer_message(message, message_id)
        except asyncio.CancelledError:
            await self._say_stopped(message_id)
            raise
        except Exception:
            await self.mark(message_id, FAILED)
            raise

    async def _say_stopped(self, message_id: str) -> None:
        """While the server shuts down: brief, and never in the way of the shutdown."""
        try:
            async with asyncio.timeout(STOPPED_NOTICE_SECONDS):
                await self.send(self._strings.chat_stopped, reply_to=message_id)
        except Exception as exc:
            log.warning("Could not say the answer was stopped: %s", exc)

    def _quoted(self, message: dict[str, Any]) -> tuple[str, str]:
        """What a reply points at, for the agent, and whose message it is ("own" for one of
        the agent's, "other" for any other, "" when nothing is quoted)."""
        quoted = message.get("reply_to_message")
        if not isinstance(quoted, dict):
            return "", ""
        number = str(quoted.get("message_id") or "")
        mine = (quoted.get("from") or {}).get("is_bot") is True
        body = " ".join(str(quoted.get("text") or quoted.get("caption") or "").split())
        shown = (self._choices.get(number) if mine else None) or body or "(a file)"
        whose = "own" if mine else "other"
        return quote_note(whose, shown), whose

    def _reply_to(self, message_id: str) -> str | None:
        """The message's id when the user wrote again after it, so the answer quotes the
        message it is about; None when it is the last one, where a quote is only noise."""
        return message_id if not self.messages.empty() else None

    async def _answer_message(self, message: dict[str, Any], message_id: str) -> None:
        if self._handler is None:
            return
        text = str(message.get("text") or message.get("caption") or "").strip()
        quoted, whose = self._quoted(message)
        forwarded = "forward_origin" in message or "forward_date" in message
        if forwarded and text:
            # Someone else's words: data the user wants read, never a request.
            text = forwarded_note("A message", text)
            whose = whose or "other"
        files = None
        found = _file_of(message)
        if found is None and not text:
            await self.send(self._unreadable(), reply_to=self._reply_to(message_id))
            return
        if found is not None:
            file = await self._api.download(*found)
            if file is None:
                await self.send(self._unreadable(), reply_to=self._reply_to(message_id))
                return
            if file.mimetype.startswith("audio/"):
                said = await transcribed(self._transcribe, file)
                if not said:
                    await self.send(
                        self._strings.chat_voice_unclear, reply_to=self._reply_to(message_id)
                    )
                    return
                if forwarded:
                    said = forwarded_note("A voice message", said)
                    whose = whose or "other"
                text = f"{VOICE_NOTE}\n{said}" + (f"\n\n{text}" if text else "")
            else:
                files = [file]
        answer = await self._handler.answer(quoted + (text or NO_CAPTION), files, whose)
        await self.send(answer, reply_to=self._reply_to(message_id))
        await self._handler.after_reply()

    def _unreadable(self) -> str:
        return self._strings.chat_file_unreadable.format(megabytes=MAX_FILE_BYTES // (1024 * 1024))

    async def _acknowledge(self, query: dict[str, Any], text: str | None = None) -> None:
        """Telegram shows the button as loading until the tap is answered."""
        try:
            await self._api.call(
                "answerCallbackQuery", callback_query_id=query.get("id"), text=text
            )
        except httpx.HTTPError as exc:
            log.warning("Could not acknowledge a tap: %s", exc)

    async def tapped(self, query: dict[str, Any]) -> None:
        """A tap on a button of a choice: the button is ticked or unticked, then the
        handler gets every option now ticked. What is ticked is read from the message's
        own buttons, so nothing has to be remembered between a choice and its taps."""
        message = query.get("message") or {}
        shown = _buttons(message)
        kind, _, index = str(query.get("data") or "").partition(":")
        if kind not in {"m", "s"} or not index.isdigit() or int(index) >= len(shown):
            # A message too old for Telegram to give its buttons, or not a choice of ours.
            await self._acknowledge(query, self._strings.chat_choice_expired)
            return
        tapped = int(index)
        options = [text.removeprefix(TICK) for text in shown]
        ticked = {n for n, text in enumerate(shown) if text.startswith(TICK)}
        if kind == "s":
            # One answer: tapping another moves it, tapping it again takes it back.
            ticked = set() if tapped in ticked else {tapped}
        else:
            ticked ^= {tapped}
        message_id = str(message.get("message_id") or "")
        try:
            await self._api.call(
                "editMessageReplyMarkup",
                chat_id=self.chat_id,
                message_id=int(message_id),
                reply_markup=_keyboard(options, kind == "m", ticked),
            )
        except httpx.HTTPError as exc:
            # Unticked on screen, so not counted: the user taps again.
            log.warning("Could not tick a button: %s", exc)
            await self._acknowledge(query)
            return
        await self._acknowledge(query)
        if self._handler is not None:
            await self._handler.chose(message_id, [options[n] for n in sorted(ticked)])

    async def reacted(self, update: dict[str, Any]) -> None:
        """A reaction the user puts on one of the agent's messages answers it, like a
        reply quoting it. A removed reaction says nothing; a choice is answered by its
        buttons."""
        message_id = str(update.get("message_id") or "")
        emojis = [
            str(r.get("emoji") or "")
            for r in update.get("new_reaction") or []
            if isinstance(r, dict) and r.get("type") == "emoji"
        ]
        if not emojis or not emojis[0] or message_id not in self._texts or self._handler is None:
            return
        text = reaction_note(emojis[0], self._texts[message_id])
        async with self.typing():
            await self.send(await self._handler.answer(text, None, "own"), reply_to=message_id)
        await self._handler.after_reply()


class TelegramBot:
    """One bot, and the chats allowed to talk to it: it asks Telegram for what was sent
    and hands each allowed chat its own messages, taps and reactions."""

    def __init__(
        self,
        api: TelegramApi,
        chat_ids: list[int],
        *,
        language: str = "en",
        transcribe: Transcribe | None = None,
        clock: Callable[[], float] = time.time,
        poll_timeout: int = POLL_TIMEOUT_SECONDS,
        retry_seconds: float = 5,
    ) -> None:
        self._api = api
        self._poll_timeout = poll_timeout
        self._retry_seconds = retry_seconds
        # Messages written while the agent was down are not answered late.
        self._since = int(clock())
        self._offset: int | None = None
        self.channels: dict[int, TelegramChannel] = {}
        for chat_id in dict.fromkeys(chat_ids):
            name = "telegram" if not self.channels else f"telegram-{chat_id}"
            self.channels[chat_id] = TelegramChannel(
                api, chat_id, name=name, language=language, transcribe=transcribe
            )

    def _channel(self, chat: Any, sender: Any) -> TelegramChannel | None:
        """The allowed chat an update belongs to: a private chat of the list, written in
        by its own user. Anything else (a stranger, a group the bot was added to) is
        nobody's."""
        chat = chat if isinstance(chat, dict) else {}
        sender = sender if isinstance(sender, dict) else {}
        channel = self.channels.get(chat.get("id"))  # type: ignore[arg-type]
        if channel is None or chat.get("type") != "private" or sender.get("id") != chat.get("id"):
            # Its id is what TELEGRAM_CHAT_IDS takes: shown so the owner can add their own.
            log.info(
                "Ignored a Telegram update from chat %s (not in TELEGRAM_CHAT_IDS)", chat.get("id")
            )
            return None
        return channel

    def dispatch(self, update: dict[str, Any]) -> None:
        if isinstance(message := update.get("message"), dict):
            channel = self._channel(message.get("chat"), message.get("from"))
            if channel is None or int(message.get("date") or 0) < self._since:
                return
            if str(message.get("text") or "").strip() == "/start":
                # What Telegram sends when the chat with a bot is opened: not a request.
                return
            channel.messages.put_nowait(message)
        elif isinstance(query := update.get("callback_query"), dict):
            channel = self._channel((query.get("message") or {}).get("chat"), query.get("from"))
            if channel is not None:
                channel.events.put_nowait(("callback", query))
        elif isinstance(reaction := update.get("message_reaction"), dict):
            channel = self._channel(reaction.get("chat"), reaction.get("user"))
            if channel is not None:
                channel.events.put_nowait(("reaction", reaction))

    async def poll_once(self) -> int:
        """Ask once for what was sent; how many updates came."""
        updates = await self._api.get_updates(self._offset, self._poll_timeout)
        for update in updates:
            if isinstance(update.get("update_id"), int):
                # Confirmed to Telegram by the next call: never handed out twice.
                self._offset = update["update_id"] + 1
            try:
                self.dispatch(update)
            except Exception:
                log.exception("Telegram update failed")
        return len(updates)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            asked = loop.time()
            try:
                came = await self.poll_once()
            except httpx.HTTPError as exc:
                log.warning("Telegram unavailable: %s", exc)
                await asyncio.sleep(self._retry_seconds)
                continue
            except Exception:
                log.exception("Telegram poll failed")
                await asyncio.sleep(self._retry_seconds)
                continue
            # Telegram holds the call open when nothing came. A server that answers at
            # once with nothing must not be asked in a tight loop.
            if not came and loop.time() - asked < MIN_POLL_SECONDS:
                await asyncio.sleep(MIN_POLL_SECONDS)
