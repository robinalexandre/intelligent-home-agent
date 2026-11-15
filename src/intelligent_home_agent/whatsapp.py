"""WhatsApp chat with the agent, through a WAHA gateway (github.com/devlikeapro/waha).

The gateway is linked either to the user's own number, the conversation then living in
their "Message yourself" chat, or to a number of the agent's own, the conversation being
its one-to-one chat with the user. In the user's own chat WhatsApp never rings, as every
message comes from their own account; with its own number the agent is a contact like any
other, and its messages notify. Which side wrote a message follows: in the user's own chat
their messages are ``fromMe``, the agent's own are told apart by the marker and the ids it
sent; in the agent's chat the user's messages are the ones that are not ``fromMe``.

A message that replies to an earlier one comes with that message quoted, as the user
means what was said there. The quote may repeat what an email said, so writes in that
request wait for the user's go, as after reading one; the agent is told whether the quote
is one of its own replies ("own") or any other message ("other"). A reaction the user puts
on one of the agent's messages (a thumbs up on a question) answers it the same way, as a
reply quoting that message.

When the user wrote again before an answer is sent (several messages in a row), the
answer quotes the message it is about, so they see which one it answers.

While the agent works on a message, the chat shows it typing: the indicator is renewed
every few seconds and dropped when the answer is sent or fails, so a stopped agent is seen
as such on the phone instead of leaving the user waiting.

Only the conversation's chat is polled, so nothing else from the account reaches the
agent. Messages the agent sends start with a marker and come back from WAHA with
``source: "api"``: in the user's own chat either keeps it from answering itself. Files
the user sends there (PDFs, images, text, office files) are downloaded on demand, for
that chat only, and passed to the agent with the message. A voice message is transcribed
on the server, when transcription is set up, and reaches the agent as what the user said.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import websockets

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

DEFAULT_MARKER = "🤖"
MAX_MESSAGES_PER_POLL = 20
MAX_FILE_BYTES = 10 * 1024 * 1024
# Files shown in the chat as a picture or a video rather than a document; a GIF stays a
# document, which keeps it moving (WhatsApp shows one sent as a picture still).
MEDIA_ENDPOINTS = {
    "image/png": "sendImage",
    "image/jpeg": "sendImage",
    "video/mp4": "sendVideo",
}
MAX_KNOWN_POLLS = 200
# The server stopped (a deploy, a restart) before answering: the message is not answered
# after the restart, so the user is told to send it again, briefly.
STOPPED_NOTICE_SECONDS = 5
# WhatsApp drops a typing indicator by itself after a while: it is renewed before that.
TYPING_REFRESH_SECONDS = 8
# Reactions the agent puts: 👀 on a poll waiting for the user, then done or failed on the
# message or poll once answered. While it works, the chat shows it typing instead.
MARKS = {WAITING: "👀", DONE: "✅", FAILED: "❌"}
STATUS_EMOJIS = frozenset(MARKS.values())
# A WhatsApp poll takes 12 options of 100 characters.
MAX_POLL_OPTIONS = 12
MAX_POLL_OPTION_CHARS = 100


def _short(message_id: str) -> str:
    """The part of a message id WAHA keeps the same whichever way it names the chat."""
    return message_id.rsplit("_", 1)[-1]


def _account(address: str) -> str:
    """The account part of a WhatsApp address, without its server or device
    (15550100:23@s.whatsapp.net is 15550100)."""
    return address.split("@", 1)[0].split(":", 1)[0]


def _forwarded(message: dict[str, Any]) -> bool:
    """Whether WhatsApp marks the message as forwarded (GOWS keeps it in the raw message)."""
    raw = ((message.get("_data") or {}).get("Message")) or {}
    return any(
        isinstance(part, dict) and (part.get("contextInfo") or {}).get("isForwarded") is True
        for part in raw.values()
    )


class Waha:
    def __init__(self, http: httpx.AsyncClient, url: str, api_key: str, session: str) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._session = session

    async def messages(
        self, chat_id: str, since: int, limit: int = MAX_MESSAGES_PER_POLL
    ) -> list[dict[str, Any]]:
        resp = await self._http.get(
            f"{self._url}/api/{self._session}/chats/{quote(chat_id)}/messages",
            params={
                "limit": limit,
                "sortOrder": "desc",
                "downloadMedia": "false",
                "filter.timestamp.gte": since,
            },
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def groups(self) -> list[dict[str, Any]]:
        """The groups and communities of the account, as WAHA's engine describes them."""
        resp = await self._http.get(
            f"{self._url}/api/{self._session}/groups", headers=self._headers, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return list(data.values()) if isinstance(data, dict) else data

    async def mark_read(self, chat_id: str, messages: int, days: int) -> list[str]:
        """Mark the chat's unread messages as read (blue ticks), at most ``messages`` of the
        last ``days``; the ids WAHA marked."""
        resp = await self._http.post(
            f"{self._url}/api/{self._session}/chats/{quote(chat_id)}/messages/read",
            json={"messages": messages, "days": days},
            headers=self._headers,
        )
        resp.raise_for_status()
        return list(resp.json().get("ids") or [])

    async def download(self, chat_id: str, message_id: str) -> Attachment | None:
        """The file of one message, or None when WAHA keeps none (a format it skips)."""
        resp = await self._http.get(
            f"{self._url}/api/{self._session}/chats/{quote(chat_id)}/messages/{message_id}",
            params={"downloadMedia": "true"},
            headers=self._headers,
        )
        resp.raise_for_status()
        media = resp.json().get("media") or {}
        if not media.get("url"):
            return None
        # WAHA gives its own internal address: fetch the same path through our URL.
        path = urlsplit(media["url"]).path
        file = await self._http.get(f"{self._url}{path}", headers=self._headers)
        file.raise_for_status()
        if len(file.content) > MAX_FILE_BYTES:
            return None
        mimetype = (media.get("mimetype") or "application/octet-stream").split(";")[0]
        return Attachment(media.get("filename") or path.rsplit("/", 1)[-1], mimetype, file.content)

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str | None:
        body: dict[str, Any] = {"session": self._session, "chatId": chat_id, "text": text}
        if reply_to:
            body["reply_to"] = reply_to
        resp = await self._http.post(f"{self._url}/api/sendText", json=body, headers=self._headers)
        resp.raise_for_status()
        return resp.json().get("id")

    async def send_poll(
        self, chat_id: str, name: str, options: list[str], multiple: bool = True
    ) -> str | None:
        resp = await self._http.post(
            f"{self._url}/api/sendPoll",
            json={
                "session": self._session,
                "chatId": chat_id,
                "poll": {"name": name, "options": options, "multipleAnswers": multiple},
            },
            headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("id") or (data.get("key") or {}).get("id")

    async def react(self, message_id: str, emoji: str) -> None:
        resp = await self._http.put(
            f"{self._url}/api/reaction",
            json={"session": self._session, "messageId": message_id, "reaction": emoji},
            headers=self._headers,
        )
        resp.raise_for_status()

    async def start_typing(self, chat_id: str) -> None:
        await self._typing("startTyping", chat_id)

    async def stop_typing(self, chat_id: str) -> None:
        await self._typing("stopTyping", chat_id)

    async def _typing(self, action: str, chat_id: str) -> None:
        resp = await self._http.post(
            f"{self._url}/api/{action}",
            json={"session": self._session, "chatId": chat_id},
            headers=self._headers,
        )
        resp.raise_for_status()

    async def events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Poll votes and reactions as they come, over WAHA's event socket (no webhook to
        set up), as (event name, payload)."""
        scheme = "wss" if self._url.startswith("https") else "ws"
        query = urlencode(
            [
                ("session", self._session),
                ("events", "poll.vote"),
                ("events", "message.reaction"),
                ("x-api-key", self._headers["X-Api-Key"]),
            ]
        )
        url = f"{scheme}://{self._url.split('://', 1)[1]}/ws?{query}"
        async with websockets.connect(url, open_timeout=15) as socket:
            async for raw in socket:
                event = json.loads(raw)
                name = str(event.get("event") or "")
                if name in {"poll.vote", "message.reaction"}:
                    yield name, event.get("payload") or {}

    async def send_file(self, chat_id: str, file: Attachment, caption: str) -> str | None:
        """Send file in the chat: a picture or a video shows as such, anything else as a
        document. When WAHA can't send that media (its engine or edition), it goes as a
        document."""
        media = MEDIA_ENDPOINTS.get(file.mimetype)
        if media:
            try:
                return await self._send_media(media, chat_id, file, caption)
            except httpx.HTTPStatusError as exc:
                log.info("WAHA %s refused %s, sent as a document: %s", media, file.filename, exc)
        return await self._send_media("sendFile", chat_id, file, caption)

    async def _send_media(
        self, endpoint: str, chat_id: str, file: Attachment, caption: str
    ) -> str | None:
        extra = {"convert": True} if endpoint == "sendVideo" else {}
        resp = await self._http.post(
            f"{self._url}/api/{endpoint}",
            json={
                **extra,
                "session": self._session,
                "chatId": chat_id,
                "caption": caption,
                "file": {
                    "mimetype": file.mimetype,
                    "filename": file.filename,
                    "data": base64.b64encode(file.data).decode(),
                },
            },
            headers=self._headers,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json().get("id")


class WhatsAppChannel:
    """The chat with the user in WhatsApp (a ``chat.ChatChannel``)."""

    name = "whatsapp"
    # Two of a poll's options are the session's own (send, cancel).
    max_options = MAX_POLL_OPTIONS
    option_chars = MAX_POLL_OPTION_CHARS

    def __init__(
        self,
        waha: Waha,
        chat_id: str,
        *,
        language: str = "en",
        marker: str = DEFAULT_MARKER,
        poll_seconds: float = 5,
        clock: Callable[[], float] = time.time,
        vote_retry_seconds: float = 30,
        typing_seconds: float = TYPING_REFRESH_SECONDS,
        transcribe: Transcribe | None = None,
        own_number: bool = False,
    ) -> None:
        self._waha = waha
        self._transcribe = transcribe
        self._strings = strings(language)
        self._marker = marker or DEFAULT_MARKER
        # The gateway is the agent's own account, chat_id the user's: not their own chat.
        self._own_number = own_number
        self._chat_id = chat_id
        self._handler: ChatHandler | None = None
        self._listening = True
        self._vote_retry_seconds = vote_retry_seconds
        self._poll_seconds = poll_seconds
        self._typing_seconds = typing_seconds
        # Messages written while the agent was down are not answered late.
        self._since = int(clock())
        self._seen: deque[str] = deque(maxlen=200)
        self._sent: deque[str] = deque(maxlen=200)
        # The polls sent, by message id: a reply to one means its question and options.
        self._polls: dict[str, str] = {}
        # The texts sent, by message id: a reaction on one answers it.
        self._texts: dict[str, str] = {}
        # The reactions the agent put itself (message id, emoji): not the user's.
        self._reacted: deque[tuple[str, str]] = deque(maxlen=200)
        # WAHA names the same chat by phone number or by its LID, depending on the engine
        # and the event: every name seen on a message of the chat is the chat.
        self._chat_names: set[str] = {_account(chat_id)}

    def bind(self, handler: ChatHandler) -> None:
        self._handler = handler

    def tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self.run(), self.listen_votes()]

    async def send(self, text: str, reply_to: str | None = None) -> str | None:
        message_id = await self._waha.send_text(self._chat_id, f"{self._marker} {text}", reply_to)
        if message_id:
            self._sent.append(message_id)
            self._texts[_short(message_id)] = text[:MAX_QUOTE_CHARS]
            while len(self._texts) > MAX_KNOWN_POLLS:
                self._texts.pop(next(iter(self._texts)))
        return message_id

    async def mark(self, message_id: str, status: str) -> None:
        await self.react(message_id, MARKS[status])

    async def react(self, message_id: str, emoji: str) -> None:
        """A reaction is a hint for the user: it never gets in the way of the answer."""
        if not message_id:
            return
        self._reacted.append((_short(message_id), emoji))
        try:
            await self._waha.react(message_id, emoji)
        except httpx.HTTPError as exc:
            log.warning("Could not react to %s: %s", message_id, exc)

    @contextlib.asynccontextmanager
    async def typing(self) -> AsyncIterator[None]:
        """Shown typing while the body runs. A hint for the user, like a reaction: WAHA
        refusing it never gets in the way of the answer."""
        keep = asyncio.create_task(self._keep_typing())
        try:
            yield
        finally:
            keep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keep
            await self._typing(self._waha.stop_typing)

    async def _keep_typing(self) -> None:
        while True:
            await self._typing(self._waha.start_typing)
            await asyncio.sleep(self._typing_seconds)

    async def _typing(self, call: Callable[[str], Awaitable[None]]) -> None:
        try:
            await call(self._chat_id)
        except httpx.HTTPError as exc:
            log.warning("Could not show typing: %s", exc)

    async def send_choices(
        self, question: str, options: list[str], multiple: bool = True
    ) -> str | None:
        """A poll the user ticks; with ``multiple`` off, one tap on an option is the answer."""
        message_id = await self._waha.send_poll(
            self._chat_id, f"{self._marker} {question}", options, multiple
        )
        if message_id:
            self._sent.append(message_id)
            self._polls[_short(message_id)] = f"poll «{question}»: " + " / ".join(options)
            while len(self._polls) > MAX_KNOWN_POLLS:
                self._polls.pop(next(iter(self._polls)))
            await self.mark(message_id, WAITING)
        return message_id

    def _learn_chat_name(self, address: Any) -> None:
        if isinstance(address, str) and "@" in address:
            self._chat_names.add(_account(address))

    def _learn_chat_names(self, message: dict[str, Any]) -> None:
        """In the user's own chat both ends are the chat. On the agent's own account only
        the user's side is: its own number would let anyone's events pass for the user's."""
        if not self._own_number:
            self._learn_chat_name(message.get("from"))
            self._learn_chat_name(message.get("to"))
        elif self._by_user(message.get("fromMe")):
            self._learn_chat_name(message.get("from"))

    def _by_user(self, from_me: Any) -> bool:
        """Whether the user's account did it: the gateway's own in their own chat, the
        other side when the agent has its own number."""
        return from_me is False if self._own_number else from_me is True

    def _sent_by_user(self, payload: dict[str, Any]) -> bool:
        """Anyone can write to the agent's own number, and its event socket brings every
        chat's votes and reactions: only those sent from the user's account count. In the
        user's own chat, being theirs (fromMe) already says so."""
        if not self._own_number:
            return True
        vote = payload.get("vote") or {}
        info = (payload.get("_data") or {}).get("Info") or {}
        senders = [
            payload.get("from"),
            payload.get("participant"),
            vote.get("from"),
            vote.get("participant"),
            info.get("Sender"),
            info.get("SenderAlt"),
        ]
        if any(_account(s) in self._chat_names for s in senders if isinstance(s, str) and s):
            return True
        log.info("Ignored a WhatsApp event from someone else")
        return False

    def _vote_from_user(self, payload: dict[str, Any]) -> bool:
        """A vote by the user on one of the agent's polls, in this chat. GOWS names the
        chat by LID in the vote and keeps the phone number in RecipientAlt."""
        vote, poll = payload.get("vote") or {}, payload.get("poll") or {}
        info = (payload.get("_data") or {}).get("Info") or {}
        where = " ".join(
            str(x or "") for x in (poll.get("to"), poll.get("id"), info.get("RecipientAlt"))
        )
        return (
            self._by_user(vote.get("fromMe"))
            and poll.get("fromMe") is True
            and any(name in where for name in self._chat_names)
            and self._sent_by_user(payload)
        )

    def _reacted_message(self, payload: dict[str, Any]) -> tuple[str, str] | None:
        """The agent's text the user reacted to, and the emoji, on a text the agent sent in
        this chat. In the user's own chat the agent's own reactions come back as theirs
        (its status marks on the user's messages and on its polls): those it put, and its
        status emojis, are never taken for the user's. A removed reaction has no emoji; a
        poll is answered by its votes, not by a reaction."""
        reaction = payload.get("reaction") or {}
        emoji = str(reaction.get("text") or "").strip()
        short = _short(str(reaction.get("messageId") or ""))
        if not emoji or not short or not self._by_user(payload.get("fromMe")):
            return None
        if emoji in STATUS_EMOJIS or (short, emoji) in self._reacted or short not in self._texts:
            return None
        if not self._sent_by_user(payload):
            return None
        for sent in self._sent:
            if _short(sent) == short:
                return sent, emoji
        return None

    async def _answer_reaction(self, payload: dict[str, Any]) -> None:
        found = self._reacted_message(payload)
        if found is None or self._handler is None:
            return
        message_id, emoji = found
        text = reaction_note(emoji, self._texts[_short(message_id)])
        try:
            async with self.typing():
                await self.send(await self._handler.answer(text, None, "own"), reply_to=message_id)
            await self._handler.after_reply()
        except Exception:
            # One failed answer must not drop the event socket.
            log.exception("WhatsApp reaction failed")

    async def listen_votes(self) -> None:
        while self._listening and self._handler is not None:
            try:
                async for name, payload in self._waha.events():
                    if name == "message.reaction":
                        await self._answer_reaction(payload)
                    elif self._vote_from_user(payload):
                        await self._handler.chose(
                            str((payload.get("poll") or {}).get("id") or ""),
                            [
                                str(o)
                                for o in (payload.get("vote") or {}).get("selectedOptions") or []
                            ],
                        )
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("WhatsApp votes unavailable: %s", exc)
            except Exception:
                log.exception("WhatsApp vote failed")
            await asyncio.sleep(self._vote_retry_seconds)

    async def send_file(self, file: Attachment) -> None:
        message_id = await self._waha.send_file(
            self._chat_id, file, f"{self._marker} {file.filename}"
        )
        if message_id:
            self._sent.append(message_id)

    def _from_user(self, message: dict[str, Any]) -> bool:
        body = (message.get("body") or "").strip()
        return (
            (bool(body) or bool(message.get("hasMedia")))
            and self._by_user(message.get("fromMe"))
            and message.get("source") != "api"
            and not body.startswith(self._marker)
            and message.get("id") not in self._sent
        )

    def _quoted(self, message: dict[str, Any]) -> tuple[str, str]:
        """What a reply points at, for the agent, and whose message it is ("own" for one of
        the agent's replies, "other" for any other, "" when nothing is quoted)."""
        quoted = message.get("replyTo") or {}
        if not isinstance(quoted, dict) or not (quoted.get("id") or quoted.get("body")):
            return "", ""
        body = " ".join(str(quoted.get("body") or "").split())
        short = _short(str(quoted.get("id") or ""))
        mine = body.startswith(self._marker) or any(
            short and _short(sent) == short for sent in self._sent
        )
        body = body.removeprefix(self._marker)
        shown = body.strip() or self._polls.get(short) or "(a poll or a file)"
        whose = "own" if mine else "other"
        return quote_note(whose, shown), whose

    async def _handle(self, message: dict[str, Any]) -> None:
        try:
            async with self.typing():
                await self._answer_message(message)
        except asyncio.CancelledError:
            await self._say_stopped(message["id"])
            raise
        except Exception:
            await self.mark(message["id"], FAILED)
            raise
        await self.mark(message["id"], DONE)

    async def _say_stopped(self, message_id: str) -> None:
        """While the server shuts down: brief, and never in the way of the shutdown."""
        try:
            async with asyncio.timeout(STOPPED_NOTICE_SECONDS):
                await self.mark(message_id, FAILED)
                await self.send(self._strings.chat_stopped, reply_to=message_id)
        except Exception as exc:
            log.warning("Could not say the answer was stopped: %s", exc)

    async def _answer_message(self, message: dict[str, Any]) -> None:
        if self._handler is None:
            return
        text = (message.get("body") or "").strip()
        quoted, whose = self._quoted(message)
        files = None
        if message.get("hasMedia"):
            file = await self._waha.download(self._chat_id, message["id"])
            if file is None:
                await self.send(
                    self._strings.chat_file_unreadable.format(
                        megabytes=MAX_FILE_BYTES // (1024 * 1024)
                    ),
                    reply_to=await self._quote_if_followed(message),
                )
                return
            if file.mimetype.startswith("audio/"):
                said = await transcribed(self._transcribe, file)
                if not said:
                    await self.send(
                        self._strings.chat_voice_unclear,
                        reply_to=await self._quote_if_followed(message),
                    )
                    return
                if _forwarded(message):
                    # Someone else's words: data the user wants heard, never a request.
                    said = forwarded_note("A voice message", said)
                    whose = whose or "other"
                text = f"{VOICE_NOTE}\n{said}" + (f"\n\n{text}" if text else "")
            else:
                files = [file]
        answer = await self._handler.answer(quoted + (text or NO_CAPTION), files, whose)
        await self.send(answer, reply_to=await self._quote_if_followed(message))
        await self._handler.after_reply()

    async def _quote_if_followed(self, message: dict[str, Any]) -> str | None:
        """The message's id when the user wrote again after it, so the answer quotes the
        message it is about; None when it is the last one, where a quote is only noise."""
        since = int(message.get("timestamp") or 0)
        try:
            later = await self._waha.messages(self._chat_id, since)
        except httpx.HTTPError as exc:
            log.warning("Could not check for newer messages: %s", exc)
            return None
        followed = any(
            m.get("id") != message["id"]
            and int(m.get("timestamp") or 0) >= since
            and self._from_user(m)
            for m in later
        )
        return message["id"] if followed else None

    async def poll_once(self) -> None:
        messages = await self._waha.messages(self._chat_id, self._since)
        for message in sorted(messages, key=lambda m: m.get("timestamp") or 0):
            message_id = message.get("id")
            if not message_id or message_id in self._seen:
                continue
            self._seen.append(message_id)
            self._since = max(self._since, int(message.get("timestamp") or 0))
            self._learn_chat_names(message)
            if self._from_user(message):
                await self._handle(message)

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except httpx.HTTPError as exc:
                log.warning("WhatsApp unavailable: %s", exc)
            except Exception:
                log.exception("WhatsApp poll failed")
            await asyncio.sleep(self._poll_seconds)
