import asyncio
import base64
import json

import httpx
import pytest
import respx

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.whatsapp import Waha, WhatsAppChannel

WAHA_URL = "http://waha.test"
CHAT = "15550100@c.us"
MESSAGES = f"{WAHA_URL}/api/me/chats/15550100%40c.us/messages"


def message(mid, ts, body, **extra):
    return {"id": mid, "timestamp": ts, "body": body, "fromMe": True, "source": "app", **extra}


def hints_accepted():
    """Reactions and the typing indicator: what the agent shows around an answer."""
    for action in ("startTyping", "stopTyping"):
        respx.post(f"{WAHA_URL}/api/{action}").mock(return_value=httpx.Response(201, json={}))
    return respx.put(f"{WAHA_URL}/api/reaction").mock(return_value=httpx.Response(200, json={}))


class Handler:
    def __init__(self, answer, on_vote=None) -> None:
        self._answer, self._on_vote = answer, on_vote
        self.replied = 0

    async def answer(self, text, files, quoted):
        return await self._answer(text, files, quoted)

    async def after_reply(self):
        self.replied += 1

    async def chose(self, choice_id, selected):
        if self._on_vote is not None:
            await self._on_vote(choice_id, selected)


def WhatsAppChat(waha, chat_id, answer, *, on_vote=None, **options):  # noqa: N802
    chat = WhatsAppChannel(waha, chat_id, **options)
    chat.bind(Handler(answer, on_vote))
    return chat


class Replies:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.files: list = []
        self.quoted: list[str] = []

    async def __call__(self, text: str, files=None, quoted="") -> str:
        self.asked.append(text)
        self.quoted.append(quoted)
        self.files.append(files)
        return f"re: {text}"


class FakeVotes(Waha):
    def __init__(self, events, messages=()) -> None:
        self._events = events
        self._messages = list(messages)
        self.sent: list[tuple[str, str | None]] = []

    async def events(self):
        for event in self._events:
            yield event

    async def messages(self, chat_id, since):
        return self._messages

    async def send_text(self, chat_id, text, reply_to=None):
        self.sent.append((text, reply_to))
        return f"true_{chat_id}_OUT{len(self.sent)}"

    async def send_poll(self, chat_id, name, options, multiple=True):
        self.sent.append((name, None))
        return f"true_{chat_id}_OUT{len(self.sent)}"

    async def react(self, message_id, emoji):
        # WAHA echoes the agent's own reactions back as events of the user's account.
        self._events.append(reaction(emoji, message_id))

    async def start_typing(self, chat_id):
        pass

    async def stop_typing(self, chat_id):
        pass


def vote(options, voter_is_me=True, poll_to=CHAT, recipient_alt=None, voter=CHAT):
    payload = {
        "vote": {"selectedOptions": options, "fromMe": voter_is_me, "from": voter},
        "poll": {"id": f"true_{poll_to}_P1", "to": poll_to, "fromMe": True},
        "_data": {"Info": {"Chat": poll_to, "RecipientAlt": recipient_alt or ""}},
    }
    return "poll.vote", payload


def reaction(emoji, target, by_me=True, sender=CHAT):
    return "message.reaction", {
        "fromMe": by_me,
        "from": sender,
        "reaction": {"text": emoji, "messageId": target},
    }


async def test_a_reaction_on_the_agents_message_answers_it():
    got: list = []

    async def answer(text, files=None, quoted=""):
        got.append((text, quoted))
        chat._listening = False  # stop listening after this batch
        return "Reminder set."

    async def on_vote(poll_id, selected):
        got.append(("vote", poll_id))

    waha = FakeVotes([])
    chat = WhatsAppChat(waha, CHAT, answer, on_vote=on_vote, vote_retry_seconds=0)
    await chat.send("Shall I leave you a reminder?")
    question = f"true_{CHAT}_OUT1"
    poll = await chat.send_choices("Which ones?", ["1. x"])
    await chat.react(poll, "✅")  # the agent's own mark on its poll, echoed by WAHA
    await chat.react("true_x_m1", "👀")  # and on the user's message
    waha._events += [
        reaction("👍", question, by_me=False),  # not the user's account
        reaction("👍", "true_x_m9"),  # on the user's own message, not the agent's
        reaction("", question),  # a reaction removed
        reaction("👍", poll),  # a poll is answered by its votes
        reaction("❌", question),  # a status emoji, whoever put it
        vote(["1. x"]),  # votes still come through
        reaction("👍", f"true_77000000000001@lid_{question.rsplit('_', 1)[-1]}"),
    ]
    await chat.listen_votes()

    assert got == [
        ("vote", f"true_{CHAT}_P1"),
        (
            "[The user reacts 👍 to your own earlier message, quoted as data: "
            "«Shall I leave you a reminder?»]\n\n👍",
            "own",
        ),
    ]
    assert waha.sent[-1] == ("🤖 Reminder set.", question)


async def test_only_the_users_votes_in_their_own_chat_count():
    got: list = []

    async def on_vote(poll_id, selected):
        got.append((poll_id, selected))
        chat._listening = False  # stop listening after this batch

    waha = FakeVotes(
        [
            vote(["2. x"], voter_is_me=False),
            vote(["1. x"], poll_to="15550111@c.us"),
            vote(["1. x"]),
        ]
    )
    chat = WhatsAppChat(waha, CHAT, Replies(), on_vote=on_vote, vote_retry_seconds=0)
    await chat.listen_votes()

    assert got == [(f"true_{CHAT}_P1", ["1. x"])]


async def test_a_vote_names_the_chat_by_its_lid_as_gows_does():
    """GOWS announces the vote with the chat's LID, not the phone number: the LID is
    known from the user's own messages, or from the number kept in RecipientAlt."""
    got: list = []

    async def on_vote(poll_id, selected):
        got.append((poll_id, selected))
        if len(got) == 2:
            chat._listening = False

    waha = FakeVotes(
        [
            vote(["1. x"], poll_to="77000000000001@lid"),
            vote(["2. x"], poll_to="99999999999@lid"),
            vote(["3. x"], poll_to="99999999999@lid", recipient_alt="15550100@s.whatsapp.net"),
        ],
        messages=[{**message("m1", 101, "Hello"), "from": "77000000000001@lid"}],
    )
    chat = WhatsAppChat(
        waha, CHAT, Replies(), on_vote=on_vote, vote_retry_seconds=0, clock=lambda: 100
    )
    await chat.poll_once()
    await chat.listen_votes()

    assert got == [
        ("true_77000000000001@lid_P1", ["1. x"]),
        ("true_99999999999@lid_P1", ["3. x"]),
    ]


@respx.mock
async def test_a_poll_is_sent_with_the_marker_and_several_answers_allowed():
    send = respx.post(f"{WAHA_URL}/api/sendPoll").mock(
        return_value=httpx.Response(201, json={"id": "P1"})
    )
    react = hints_accepted()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, Replies())
        assert await chat.send_choices("Which ones?", ["1. a", "2. b"]) == "P1"
        assert await chat.send_choices(
            "Tuesday or Thursday?", ["Tuesday", "Thursday"], multiple=False
        )

    assert json.loads(send.calls[0].request.content)["poll"] == {
        "name": "🤖 Which ones?",
        "options": ["1. a", "2. b"],
        "multipleAnswers": True,
    }
    assert json.loads(send.calls[1].request.content)["poll"]["multipleAnswers"] is False
    # The poll waits for the user: it says so until the agent acts on the answer.
    assert json.loads(react.calls[0].request.content)["reaction"] == "👀"


@respx.mock
async def test_marking_a_group_read_asks_waha_for_its_unread_messages():
    read = respx.post(f"{WAHA_URL}/api/me/chats/200%40g.us/messages/read").mock(
        return_value=httpx.Response(200, json={"ids": ["false_200@g.us_A_1555@c.us"]})
    )
    async with httpx.AsyncClient() as http:
        ids = await Waha(http, WAHA_URL, "key", "me").mark_read("200@g.us", 200, 7)

    assert ids == ["false_200@g.us_A_1555@c.us"]
    assert json.loads(read.calls[0].request.content) == {"messages": 200, "days": 7}


@respx.mock
async def test_a_reply_brings_the_message_it_points_at():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(
            200,
            json=[
                message(
                    "m1", 101, "Yes, that slot", replyTo={"id": "out7", "body": "🤖 Tuesday 10am?"}
                ),
                message(
                    "m2",
                    102,
                    "That one",
                    replyTo={"id": "true_x_m0", "body": "My shelf quote"},
                ),
                message("m3", 103, "Just this"),
            ],
        )
    )
    respx.post(f"{WAHA_URL}/api/sendText").mock(return_value=httpx.Response(201, json={}))
    hints_accepted()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.poll_once()

    assert replies.asked == [
        "[The user replies to your own earlier message, quoted as data: «Tuesday 10am?»]\n\n"
        "Yes, that slot",
        "[The user replies to their own earlier message, quoted as data: «My shelf quote»]\n\n"
        "That one",
        "Just this",
    ]
    assert replies.quoted == ["own", "other", ""]


@respx.mock
async def test_answers_only_what_the_user_typed():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(
            200,
            json=[
                message("m5", 105, "  And tomorrow?  "),
                message("m4", 104, "🤖 This morning's briefing"),
                message("m3", 103, "Reply sent by the API", source="api"),
                message("m2", 102, "", hasMedia=True, source="api"),
                message("m1", 101, "Someone else's message", fromMe=False),
            ],
        )
    )
    send = respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.poll_once()

    assert replies.asked == ["And tomorrow?"]
    payload = json.loads(send.calls[0].request.content)
    assert payload == {"session": "me", "chatId": CHAT, "text": "🤖 re: And tomorrow?"}
    assert send.calls[0].request.headers["x-api-key"] == "key"


@respx.mock
async def test_an_answer_quotes_its_message_only_when_the_user_wrote_again():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(
            200,
            json=[
                message("m2", 102, "And the weather?"),
                message("m1", 101, "File this quote"),
                message("out1", 103, "🤖 re: File this quote", source="api"),
            ],
        )
    )
    send = respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, Replies(), clock=lambda: 100)
        await chat.poll_once()

    sent = [json.loads(c.request.content) for c in send.calls]
    assert [(s["text"], s.get("reply_to")) for s in sent] == [
        ("🤖 re: File this quote", "m1"),
        ("🤖 re: And the weather?", None),
    ]


@respx.mock
async def test_the_users_message_gets_a_check_once_answered():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("m1", 101, "File this quote")])
    )
    respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    react = respx.put(f"{WAHA_URL}/api/reaction").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, Replies(), clock=lambda: 100)
        await chat.poll_once()  # a failed reaction never breaks the answer

    # No 👀 while it works: the typing indicator says so. The check comes once answered.
    assert [json.loads(c.request.content) for c in react.calls] == [
        {"session": "me", "messageId": "m1", "reaction": "✅"},
    ]


@respx.mock
async def test_the_agent_is_shown_typing_while_it_works_then_not():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("m1", 101, "File this quote")])
    )
    respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    start = respx.post(f"{WAHA_URL}/api/startTyping").mock(
        side_effect=lambda request, route: httpx.Response(500 if route.call_count == 0 else 201)
    )
    stop = respx.post(f"{WAHA_URL}/api/stopTyping").mock(return_value=httpx.Response(201, json={}))

    async def slow_answer(text, files=None, quoted=""):
        await asyncio.sleep(0.05)
        return "Filed."

    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(
            Waha(http, WAHA_URL, "key", "me"),
            CHAT,
            slow_answer,
            clock=lambda: 100,
            typing_seconds=0.02,
        )
        await chat.poll_once()  # a refused indicator never breaks the answer

    assert len(start.calls) >= 2  # renewed while the answer takes time
    assert json.loads(start.calls[0].request.content) == {"session": "me", "chatId": CHAT}
    assert len(stop.calls) == 1
    assert stop.calls[0].request.content == start.calls[0].request.content


@respx.mock
async def test_typing_stops_when_the_answer_fails():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("m1", 101, "File this quote")])
    )
    hints_accepted()
    stop = respx.post(f"{WAHA_URL}/api/stopTyping").mock(return_value=httpx.Response(201, json={}))

    async def broken(text, files=None, quoted=""):
        raise RuntimeError("boom")

    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, broken, clock=lambda: 100)
        with pytest.raises(RuntimeError):
            await chat.poll_once()

    assert len(stop.calls) == 1


@respx.mock
async def test_each_message_is_answered_once_and_old_ones_are_ignored():
    route = respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("m1", 100, "Hello")])
    )
    respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.poll_once()
        await chat.poll_once()

    assert replies.asked == ["Hello"]
    assert route.calls[0].request.url.params["filter.timestamp.gte"] == "100"
    assert route.calls[0].request.url.params["downloadMedia"] == "false"


@respx.mock
async def test_a_file_from_the_user_is_downloaded_and_passed_on():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(
            200, json=[message("m1", 101, "Write an email with this quote", hasMedia=True)]
        )
    )
    one = respx.get(f"{MESSAGES}/m1").mock(
        return_value=httpx.Response(
            200,
            json={
                "media": {
                    "url": "http://localhost:3000/api/files/me/m1.pdf",
                    "filename": "quote.pdf",
                    "mimetype": "application/pdf",
                }
            },
        )
    )
    respx.get(f"{WAHA_URL}/api/files/me/m1.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7")
    )
    respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.poll_once()

    assert replies.asked == ["Write an email with this quote"]
    (file,) = replies.files[0]
    assert (file.filename, file.mimetype, file.data) == (
        "quote.pdf",
        "application/pdf",
        b"%PDF-1.7",
    )
    assert one.calls[0].request.url.params["downloadMedia"] == "true"


@respx.mock
async def test_a_file_waha_skipped_gets_a_short_explanation():
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("m1", 101, "", hasMedia=True)])
    )
    respx.get(f"{MESSAGES}/m1").mock(
        return_value=httpx.Response(200, json={"media": {"url": None}})
    )
    send = respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.poll_once()

    assert replies.asked == []
    assert "PDF, images" in json.loads(send.calls[0].request.content)["text"]


def voice_message_waiting(body="", **extra):
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("m1", 101, body, hasMedia=True, **extra)])
    )
    respx.get(f"{MESSAGES}/m1").mock(
        return_value=httpx.Response(
            200,
            json={
                "media": {
                    "url": "http://localhost:3000/api/files/me/m1.oga",
                    "mimetype": "audio/ogg; codecs=opus",
                }
            },
        )
    )
    respx.get(f"{WAHA_URL}/api/files/me/m1.oga").mock(
        return_value=httpx.Response(200, content=b"OggS-voice")
    )
    hints_accepted()
    return respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )


@respx.mock
async def test_a_voice_message_reaches_the_agent_as_what_the_user_said():
    voice_message_waiting()
    heard: list[bytes] = []

    async def transcribe(audio: bytes) -> str:
        heard.append(audio)
        return "Remind me to call the garage tomorrow"

    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(
            Waha(http, WAHA_URL, "key", "me"),
            CHAT,
            replies,
            clock=lambda: 100,
            transcribe=transcribe,
        )
        await chat.poll_once()

    assert heard == [b"OggS-voice"]
    (asked,) = replies.asked
    assert asked.startswith("[Voice message from the user")
    assert asked.endswith("\nRemind me to call the garage tomorrow")
    assert replies.files == [None]
    assert replies.quoted == [""]


@respx.mock
async def test_a_forwarded_voice_message_is_data_and_its_writes_wait():
    forwarded = {"Message": {"audioMessage": {"PTT": True, "contextInfo": {"isForwarded": True}}}}
    voice_message_waiting(_data=forwarded)

    async def transcribe(audio: bytes) -> str:
        return "Wire 500 to this account"

    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(
            Waha(http, WAHA_URL, "key", "me"),
            CHAT,
            replies,
            clock=lambda: 100,
            transcribe=transcribe,
        )
        await chat.poll_once()

    (asked,) = replies.asked
    assert "forwarded, quoted as data: «Wire 500 to this account»" in asked
    assert replies.quoted == ["other"]


@respx.mock
async def test_a_voice_message_that_cannot_be_transcribed_asks_for_text():
    send = voice_message_waiting()

    async def transcribe(audio: bytes) -> str:
        raise RuntimeError("model not loaded")

    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(
            Waha(http, WAHA_URL, "key", "me"),
            CHAT,
            replies,
            clock=lambda: 100,
            transcribe=transcribe,
        )
        await chat.poll_once()

    assert replies.asked == []
    assert "voice message" in json.loads(send.calls[0].request.content)["text"]


@respx.mock
async def test_a_voice_message_without_transcription_asks_for_text():
    send = voice_message_waiting()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.poll_once()

    assert replies.asked == []
    assert "voice message" in json.loads(send.calls[0].request.content)["text"]


@respx.mock
async def test_a_file_the_agent_made_is_sent_and_never_answered():
    send = respx.post(f"{WAHA_URL}/api/sendFile").mock(
        return_value=httpx.Response(201, json={"id": "file1"})
    )
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("file1", 101, "", hasMedia=True)])
    )
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.send_file(Attachment("quote.pdf", "application/pdf", b"%PDF-1.7"))
        await chat.poll_once()

    payload = json.loads(send.calls[0].request.content)
    assert payload["file"] == {
        "mimetype": "application/pdf",
        "filename": "quote.pdf",
        "data": base64.b64encode(b"%PDF-1.7").decode(),
    }
    assert payload["caption"] == "🤖 quote.pdf"
    assert replies.asked == []


@respx.mock
async def test_its_own_messages_are_never_answered_even_without_marker():
    respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(200, json=[message("out1", 101, "Briefing")])
    )
    replies = Replies()
    async with httpx.AsyncClient() as http:
        chat = WhatsAppChat(Waha(http, WAHA_URL, "key", "me"), CHAT, replies, clock=lambda: 100)
        await chat.send("Briefing")
        await chat.poll_once()

    assert replies.asked == []


@respx.mock
async def test_with_its_own_number_the_agent_answers_what_the_user_sends_it():
    """On the agent's own account the user's messages come from the other side."""
    respx.get(MESSAGES).mock(
        return_value=httpx.Response(
            200,
            json=[
                message("m2", 102, "And tomorrow?", fromMe=False),
                message("m1", 101, "Briefing without a marker"),
            ],
        )
    )
    send = respx.post(f"{WAHA_URL}/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    hints_accepted()
    replies = Replies()
    async with httpx.AsyncClient() as http:
        waha = Waha(http, WAHA_URL, "key", "me")
        chat = WhatsAppChat(waha, CHAT, replies, clock=lambda: 100, own_number=True)
        await chat.poll_once()

    assert replies.asked == ["And tomorrow?"]
    assert json.loads(send.calls[0].request.content)["chatId"] == CHAT


async def test_with_its_own_number_only_the_users_votes_and_reactions_count():
    got: list = []

    async def answer(text, files=None, quoted=""):
        got.append(text)
        chat._listening = False  # stop listening after this batch
        return "Reminder set."

    async def on_vote(poll_id, selected):
        got.append(selected)

    waha = FakeVotes([])
    chat = WhatsAppChat(waha, CHAT, answer, on_vote=on_vote, vote_retry_seconds=0, own_number=True)
    await chat.send("Shall I leave you a reminder?")
    question = f"true_{CHAT}_OUT1"
    await chat.react(question, "🙂")  # the agent's own reaction: its account, not the user's
    stranger = "15550199@c.us"
    waha._events += [
        vote(["2. x"]),  # the agent's account voting
        vote(["3. x"], voter_is_me=False, voter=stranger),  # someone else who wrote to it
        reaction("👎", question, by_me=False, sender=stranger),
        vote(["1. x"], voter_is_me=False),
        reaction("👍", question, by_me=False),
    ]
    await chat.listen_votes()

    assert got == [
        ["1. x"],
        "[The user reacts 👍 to your own earlier message, quoted as data: "
        "«Shall I leave you a reminder?»]\n\n👍",
    ]


async def test_with_its_own_number_the_user_is_known_by_lid_but_never_the_agent():
    """GOWS may name the user by LID: learned from their messages. The agent's own
    number, on its own messages, never becomes a name of the user."""
    got: list = []

    async def on_vote(poll_id, selected):
        got.append(selected)
        if len(got) == 1:
            chat._listening = False

    agent = "15550177@c.us"
    waha = FakeVotes(
        [
            vote(["1. x"], voter_is_me=False, poll_to=agent, voter=agent),
            vote(["2. x"], voter_is_me=False, voter="77000000000001@lid"),
        ],
        messages=[
            {**message("m1", 101, "Hello", fromMe=False), "from": "77000000000001@lid"},
            {**message("out1", 102, "🤖 Hello"), "from": agent, "to": CHAT},
        ],
    )
    chat = WhatsAppChat(
        waha,
        CHAT,
        Replies(),
        on_vote=on_vote,
        vote_retry_seconds=0,
        clock=lambda: 100,
        own_number=True,
    )
    await chat.poll_once()
    await chat.listen_votes()

    assert got == [["2. x"]]


async def test_an_answer_cut_by_a_restart_tells_the_user_to_send_it_again():
    started = asyncio.Event()

    async def answer(text, files=None, quoted=""):
        started.set()
        await asyncio.sleep(3600)

    waha = FakeVotes([])
    chat = WhatsAppChat(waha, CHAT, answer)
    handling = asyncio.create_task(chat._handle(message("true_x_m1", 1, "Wind at North Beach?")))
    await started.wait()
    handling.cancel()  # the server shuts down for a deploy
    with pytest.raises(asyncio.CancelledError):
        await handling

    assert waha.sent == [(f"🤖 {strings('en').chat_stopped}", "true_x_m1")]
    assert waha._events[-1] == reaction("❌", "true_x_m1")


@respx.mock
async def test_pictures_and_videos_show_as_media_else_go_as_documents():
    image = respx.post(f"{WAHA_URL}/api/sendImage").mock(
        return_value=httpx.Response(201, json={"id": "img1"})
    )
    video = respx.post(f"{WAHA_URL}/api/sendVideo").mock(return_value=httpx.Response(422))
    document = respx.post(f"{WAHA_URL}/api/sendFile").mock(
        return_value=httpx.Response(201, json={"id": "doc1"})
    )
    async with httpx.AsyncClient() as http:
        waha = Waha(http, WAHA_URL, "key", "me")
        assert await waha.send_file(CHAT, Attachment("a.png", "image/png", b"x"), "c") == "img1"
        # This WAHA can't send videos: the clip still arrives, as a document.
        clip = Attachment("a.mp4", "video/mp4", b"y")
        assert await waha.send_file(CHAT, clip, "c") == "doc1"
        assert await waha.send_file(CHAT, Attachment("a.gif", "image/gif", b"z"), "c") == "doc1"

    assert json.loads(video.calls[0].request.content)["convert"] is True
    assert image.call_count == 1 and document.call_count == 2
