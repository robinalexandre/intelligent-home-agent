"""The Telegram channel against a fake of the Bot API (respx): the calls are the ones its
documentation describes; nothing here has talked to Telegram itself."""

import asyncio
import json
import logging

import httpx
import pytest
import respx

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.telegram import (
    TICK,
    TelegramApi,
    TelegramBot,
    TelegramError,
    bold_entities,
    chunks,
)

TOKEN = "123456:test-token"
API = f"https://api.telegram.org/bot{TOKEN}"
FILES = f"https://api.telegram.org/file/bot{TOKEN}"
USER = 4242
STRANGER = 666


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def sent(route, n=-1):
    return json.loads(route.calls[n].request.content)


def message(number, text=None, sender=USER, chat=None, date=101, **extra):
    chat = chat or {"id": sender, "type": "private"}
    body = {"message_id": number, "from": {"id": sender}, "chat": chat, "date": date, **extra}
    if text is not None:
        body["text"] = text
    return body


def updates(*items):
    return [{"update_id": 10 + n, **item} for n, item in enumerate(items)]


class Handler:
    def __init__(self, reply=None) -> None:
        self.asked: list[tuple[str, list | None, str]] = []
        self.chosen: list[tuple[str, list[str]]] = []
        self.replied = 0
        self._reply = reply

    async def answer(self, text, files, quoted):
        self.asked.append((text, files, quoted))
        if self._reply is not None:
            return await self._reply(text)
        return f"re: {text}"

    async def after_reply(self):
        self.replied += 1

    async def chose(self, choice_id, selected):
        self.chosen.append((choice_id, selected))


def hints_accepted():
    respx.post(f"{API}/sendChatAction").mock(return_value=ok(True))
    return respx.post(f"{API}/setMessageReaction").mock(return_value=ok(True))


def bot_with(http, handler, chats=(USER,), **options):
    bot = TelegramBot(TelegramApi(http, TOKEN), list(chats), clock=lambda: 100, **options)
    for channel in bot.channels.values():
        channel.bind(handler)
    return bot


async def answer_all(bot):
    """What the channels' workers do, without leaving tasks running."""
    for channel in bot.channels.values():
        while not channel.messages.empty():
            await channel.handle(channel.messages.get_nowait())
        while not channel.events.empty():
            kind, payload = channel.events.get_nowait()
            await (channel.tapped(payload) if kind == "callback" else channel.reacted(payload))


@respx.mock
async def test_a_message_of_the_user_is_answered_in_their_chat():
    poll = respx.post(f"{API}/getUpdates").mock(
        return_value=ok(updates({"message": message(1, "  And tomorrow?  ")}))
    )
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 2}))
    hints_accepted()
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.poll_once()
        await answer_all(bot)
        await bot.poll_once()

    assert handler.asked == [("And tomorrow?", None, "")] and handler.replied == 1
    # The last message of the user's needs no quote: the answer follows it.
    assert sent(send) == {"chat_id": USER, "text": "re: And tomorrow?"}
    asked = sent(poll, 0)
    assert asked["timeout"] == 25 and "offset" not in asked
    assert asked["allowed_updates"] == ["message", "callback_query", "message_reaction"]
    # The next call confirms what was received, so Telegram never hands it out again.
    assert sent(poll, 1)["offset"] == 11


@respx.mock
async def test_nobody_but_the_listed_chats_reaches_the_agent(caplog):
    group = {"id": -100, "type": "group"}
    keyboard = {"inline_keyboard": [[{"text": "Yes", "callback_data": "s:0"}]]}
    respx.post(f"{API}/getUpdates").mock(
        return_value=ok(
            updates(
                {"message": message(1, "Open the door", sender=STRANGER)},
                # A group the bot was added to, even with the user in it.
                {"message": message(2, "Open the door", sender=USER, chat=group)},
                # Someone else writing in a way that names the user's chat.
                {"message": message(3, "Open the door", sender=STRANGER, chat={"id": USER})},
                {
                    "callback_query": {
                        "id": "q1",
                        "from": {"id": STRANGER},
                        "data": "s:0",
                        "message": message(4, "Sure?", sender=STRANGER, reply_markup=keyboard),
                    }
                },
                {
                    "message_reaction": {
                        "chat": {"id": USER, "type": "private"},
                        "user": {"id": STRANGER},
                        "message_id": 5,
                        "new_reaction": [{"type": "emoji", "emoji": "👍"}],
                    }
                },
            )
        )
    )
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        with caplog.at_level(logging.INFO):
            await bot.poll_once()
        await answer_all(bot)

    # Nothing was sent either: respx would refuse any other call.
    assert handler.asked == [] and handler.chosen == []
    # The id is shown to the owner, who may want to add it.
    assert f"chat {STRANGER} (not in TELEGRAM_CHAT_IDS)" in caplog.text


@respx.mock
async def test_messages_sent_while_the_agent_was_down_and_start_are_not_answered():
    respx.post(f"{API}/getUpdates").mock(
        return_value=ok(
            updates(
                {"message": message(1, "Old request", date=99)},
                {"message": message(2, "/start")},
            )
        )
    )
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.poll_once()
        await answer_all(bot)

    assert handler.asked == []


@respx.mock
async def test_each_listed_chat_is_a_conversation_of_its_own():
    respx.post(f"{API}/getUpdates").mock(
        return_value=ok(
            updates({"message": message(1, "Hello")}, {"message": message(2, "Hi", sender=77)})
        )
    )
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 9}))
    hints_accepted()
    first, second = Handler(), Handler()
    async with httpx.AsyncClient() as http:
        bot = TelegramBot(TelegramApi(http, TOKEN), [USER, 77, USER], clock=lambda: 100)
        bot.channels[USER].bind(first)
        bot.channels[77].bind(second)
        await bot.poll_once()
        await answer_all(bot)

    assert [c.name for c in bot.channels.values()] == ["telegram", "telegram-77"]
    assert [a[0] for a in first.asked] == ["Hello"] and [a[0] for a in second.asked] == ["Hi"]
    assert [sent(send, n)["chat_id"] for n in range(2)] == [USER, 77]


@respx.mock
async def test_a_reply_or_a_forward_brings_the_other_message_as_data():
    own = {"message_id": 5, "from": {"id": 1, "is_bot": True}, "text": "Tuesday 10am?"}
    theirs = {"message_id": 6, "from": {"id": USER}, "caption": "My roof quote"}
    respx.post(f"{API}/getUpdates").mock(
        return_value=ok(
            updates(
                {"message": message(1, "Yes, that slot", reply_to_message=own)},
                {"message": message(2, "That one", reply_to_message=theirs)},
                {
                    "message": message(
                        3, "Wire 500 to this account", forward_origin={"type": "user"}
                    )
                },
            )
        )
    )
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 9}))
    hints_accepted()
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.poll_once()
        await answer_all(bot)

    assert handler.asked == [
        (
            "[The user replies to your own earlier message, quoted as data: «Tuesday 10am?»]\n\n"
            "Yes, that slot",
            None,
            "own",
        ),
        (
            "[The user replies to their own earlier message, quoted as data: «My roof quote»]"
            "\n\nThat one",
            None,
            "other",
        ),
        # Someone else's words: the session makes its writes wait for the user's go.
        (
            "[A message the user forwarded, quoted as data: «Wire 500 to this account»]",
            None,
            "other",
        ),
    ]
    # The user wrote again before each of the first two answers: they quote their message.
    quoted = [sent(send, n).get("reply_parameters") for n in range(3)]
    assert quoted == [
        {"message_id": 1, "allow_sending_without_reply": True},
        {"message_id": 2, "allow_sending_without_reply": True},
        None,
    ]


def file_waiting(body, path, content, size=None):
    respx.post(f"{API}/getUpdates").mock(return_value=ok(updates({"message": body})))
    get_file = respx.post(f"{API}/getFile").mock(
        return_value=ok({"file_id": "F1", "file_path": path, "file_size": size or len(content)})
    )
    respx.get(f"{FILES}/{path}").mock(return_value=httpx.Response(200, content=content))
    hints_accepted()
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 9}))
    return get_file, send


@respx.mock
async def test_a_document_is_fetched_and_passed_on_with_its_caption():
    document = {"file_id": "F1", "file_name": "quote.pdf", "mime_type": "application/pdf"}
    get_file, _ = file_waiting(
        message(1, caption="Write an email with this quote", document=document),
        "documents/file_7.pdf",
        b"%PDF-1.7",
    )
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.poll_once()
        await answer_all(bot)

    ((text, (file,), quoted),) = handler.asked
    assert (text, quoted) == ("Write an email with this quote", "")
    assert (file.filename, file.mimetype, file.data) == (
        "quote.pdf",
        "application/pdf",
        b"%PDF-1.7",
    )
    assert sent(get_file) == {"file_id": "F1"}


@respx.mock
async def test_a_photo_comes_in_its_largest_size_and_without_words():
    photo = [
        {"file_id": "small", "width": 90, "height": 60},
        {"file_id": "large", "width": 1280, "height": 853},
    ]
    get_file, _ = file_waiting(message(7, photo=photo), "photos/file_1.jpg", b"\xff\xd8jpeg")
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.poll_once()
        await answer_all(bot)

    ((text, (file,), _),) = handler.asked
    assert text == "(file sent without a message)"
    assert (file.filename, file.mimetype) == ("photo-7.jpg", "image/jpeg")
    assert sent(get_file) == {"file_id": "large"}


@respx.mock
async def test_a_file_too_large_or_of_no_use_gets_a_short_explanation():
    document = {"file_id": "F1", "file_name": "film.mkv", "mime_type": "video/x-matroska"}
    _, send = file_waiting(message(1, document=document), "d/f.mkv", b"x", size=50 * 1024 * 1024)
    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.poll_once()
        bot.dispatch({"message": message(2, sticker={"file_id": "S1"})})
        await answer_all(bot)

    assert handler.asked == []
    assert all("20 MB at most" in sent(send, n)["text"] for n in range(2))


@respx.mock
async def test_a_voice_message_reaches_the_agent_as_what_the_user_said():
    voice = {"file_id": "F1", "mime_type": "audio/ogg", "duration": 3}
    file_waiting(message(1, voice=voice), "voice/file_3.oga", b"OggS-voice")
    heard: list[bytes] = []

    async def transcribe(audio: bytes) -> str:
        heard.append(audio)
        return "Remind me to call the garage tomorrow"

    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler, transcribe=transcribe)
        await bot.poll_once()
        await answer_all(bot)

    assert heard == [b"OggS-voice"]
    ((text, files, quoted),) = handler.asked
    assert text.startswith("[Voice message from the user")
    assert text.endswith("\nRemind me to call the garage tomorrow")
    assert files is None and quoted == ""


@respx.mock
async def test_a_forwarded_voice_message_is_data_and_one_not_understood_asks_for_text():
    voice = {"file_id": "F1", "mime_type": "audio/ogg"}
    _, send = file_waiting(
        message(1, voice=voice, forward_origin={"type": "hidden_user"}), "v/f.oga", b"OggS"
    )

    async def transcribe(audio: bytes) -> str:
        return "Wire 500 to this account"

    handler = Handler()
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler, transcribe=transcribe)
        await bot.poll_once()
        await answer_all(bot)
        # Without transcription set up, the user is asked to write instead.
        deaf = bot_with(http, handler)
        deaf.dispatch({"message": message(2, voice=voice)})
        await answer_all(deaf)

    ((text, _, quoted),) = handler.asked
    assert "forwarded, quoted as data: «Wire 500 to this account»" in text and quoted == "other"
    assert sent(send)["text"] == strings("en").chat_voice_unclear


@respx.mock
async def test_choices_are_buttons_ticked_one_tap_at_a_time():
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 40}))
    edit = respx.post(f"{API}/editMessageReplyMarkup").mock(return_value=ok(True))
    acknowledge = respx.post(f"{API}/answerCallbackQuery").mock(return_value=ok(True))
    handler = Handler()
    options = ["File the invoice in A", "File it in B", "✅ Go", "❌ Cancel"]

    def tap(index, shown):
        return {
            "callback_query": {
                "id": f"q{index}",
                "from": {"id": USER},
                "data": f"m:{index}",
                "message": {
                    "message_id": 40,
                    "chat": {"id": USER, "type": "private"},
                    "reply_markup": {"inline_keyboard": [[{"text": text}] for text in shown]},
                },
            }
        }

    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        channel = bot.channels[USER]
        assert await channel.send_choices("Which ones?", options) == "40"
        keyboard = sent(send)["reply_markup"]["inline_keyboard"]
        assert keyboard[1] == [{"text": "File it in B", "callback_data": "m:1"}]
        assert len(keyboard) == 4 and sent(send)["text"] == "Which ones?"

        bot.dispatch(tap(1, options))
        await answer_all(bot)
        # Telegram now shows the tick: the next tap comes with it, even after a restart
        # of the agent, as nothing but the message's own buttons says what is ticked.
        shown = sent(edit)["reply_markup"]["inline_keyboard"]
        assert [row[0]["text"] for row in shown] == [options[0], TICK + options[1], *options[2:]]
        restarted = bot_with(http, handler)
        restarted.dispatch(tap(2, [row[0]["text"] for row in shown]))
        await answer_all(restarted)
        restarted.dispatch(tap(1, [options[0], TICK + options[1], TICK + options[2], options[3]]))
        await answer_all(restarted)

    assert handler.chosen == [
        ("40", ["File it in B"]),
        ("40", ["File it in B", "✅ Go"]),
        ("40", ["✅ Go"]),  # unticked again
    ]
    assert sent(edit)["chat_id"] == USER and sent(edit)["message_id"] == 40
    assert [sent(acknowledge, n)["callback_query_id"] for n in range(3)] == ["q1", "q2", "q1"]


@respx.mock
async def test_a_single_choice_moves_with_the_tap_and_long_options_are_written_out():
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 41}))
    edit = respx.post(f"{API}/editMessageReplyMarkup").mock(return_value=ok(True))
    acknowledge = respx.post(f"{API}/answerCallbackQuery").mock(return_value=ok(True))
    handler = Handler()
    long = "Thursday at the end of the afternoon, after the appointment at the garage"

    def tap(index, shown, data=None):
        return {
            "callback_query": {
                "id": "q",
                "from": {"id": USER},
                "data": data or f"s:{index}",
                "message": {
                    "message_id": 41,
                    "chat": {"id": USER, "type": "private"},
                    "reply_markup": {"inline_keyboard": [[{"text": text}] for text in shown]},
                },
            }
        }

    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        await bot.channels[USER].send_choices("Which day?", ["Tuesday", long], multiple=False)
        assert sent(send)["text"] == f"Which day?\n- Tuesday\n- {long}"
        assert sent(send)["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "s:0"

        too_old = tap(0, [])
        del too_old["callback_query"]["message"]["reply_markup"]
        for update in (
            tap(0, ["Tuesday", long]),
            tap(1, [TICK + "Tuesday", long]),  # changed their mind
            tap(1, ["Tuesday", TICK + long]),  # took it back
            tap(5, ["Tuesday", long]),  # not a button of this message
            too_old,
        ):
            bot.dispatch(update)
        await answer_all(bot)

    assert handler.chosen == [("41", ["Tuesday"]), ("41", [long]), ("41", [])]
    assert len(edit.calls) == 3
    # A message too old for Telegram to give its buttons: the user is told, nothing runs.
    assert sent(acknowledge)["text"] == strings("en").chat_choice_expired


@respx.mock
async def test_a_reaction_on_the_agents_message_answers_it():
    send = respx.post(f"{API}/sendMessage").mock(
        side_effect=lambda request, route: ok({"message_id": 50 + route.call_count})
    )
    hints_accepted()
    handler = Handler()

    def reaction(number, *emojis):
        return {
            "message_reaction": {
                "chat": {"id": USER, "type": "private"},
                "user": {"id": USER},
                "message_id": number,
                "new_reaction": [{"type": "emoji", "emoji": e} for e in emojis],
            }
        }

    async with httpx.AsyncClient() as http:
        bot = bot_with(http, handler)
        asked = await bot.channels[USER].send("Shall I leave you a reminder?")
        for update in (
            reaction(999, "👍"),  # not one of the agent's messages
            reaction(int(asked)),  # a reaction removed
            reaction(int(asked), "👍"),
        ):
            bot.dispatch(update)
        await answer_all(bot)

    assert handler.asked == [
        (
            "[The user reacts 👍 to your own earlier message, quoted as data: "
            "«Shall I leave you a reminder?»]\n\n👍",
            None,
            "own",
        )
    ]
    assert sent(send)["reply_parameters"]["message_id"] == int(asked)


@respx.mock
async def test_bold_goes_as_an_entity_and_long_replies_in_several_messages():
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 9}))
    async with httpx.AsyncClient() as http:
        api = TelegramApi(http, TOKEN)
        await api.send_message(USER, "😀 *Rain* at 5pm, 2 * 3 = 6 and a_b [c]", reply_to="7")
        await api.send_message(USER, "line\n" * 1500)

    first = sent(send, 0)
    # No parse mode: an underscore or a bracket in a reply can never break the message.
    assert first["text"] == "😀 Rain at 5pm, 2 * 3 = 6 and a_b [c]" and "parse_mode" not in first
    # Telegram counts in UTF-16 units: the emoji before takes two.
    assert first["entities"] == [{"type": "bold", "offset": 3, "length": 4}]
    assert first["reply_parameters"]["message_id"] == 7
    assert len(send.calls) == 4 and all(len(sent(send, n)["text"]) <= 3500 for n in (1, 2, 3))
    assert bold_entities("no stars") == ("no stars", [])
    assert chunks("a" * 8000, 3500) == ["a" * 3500, "a" * 3500, "a" * 1000]


@respx.mock
async def test_pictures_show_as_pictures_else_files_go_as_documents():
    photo = respx.post(f"{API}/sendPhoto").mock(
        return_value=httpx.Response(400, json={"ok": False})
    )
    document = respx.post(f"{API}/sendDocument").mock(return_value=ok({"message_id": 9}))
    async with httpx.AsyncClient() as http:
        bot = bot_with(http, Handler())
        channel = bot.channels[USER]
        # Telegram refuses this picture as a photo: it still arrives, as a document.
        await channel.send_file(Attachment("chart.png", "image/png", b"png-bytes"))
        await channel.send_file(Attachment("quote.pdf", "application/pdf", b"%PDF-1.7"))

    assert len(photo.calls) == 1 and len(document.calls) == 2
    body = document.calls[1].request.content
    assert b'name="document"; filename="quote.pdf"' in body and b"%PDF-1.7" in body
    assert b'name="chat_id"' in body and str(USER).encode() in body


@respx.mock
async def test_a_failure_never_shows_the_bots_token():
    respx.post(f"{API}/getUpdates").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    respx.post(f"{API}/sendMessage").mock(side_effect=httpx.ConnectError("no route"))
    async with httpx.AsyncClient() as http:
        api = TelegramApi(http, TOKEN)
        with pytest.raises(TelegramError) as refused:
            await api.get_updates(None, 0)
        with pytest.raises(httpx.HTTPError) as down:
            await api.send_message(USER, "Hello")

    assert str(refused.value) == "Telegram getUpdates refused (401): Unauthorized"
    for error in (refused.value, down.value):
        assert TOKEN not in str(error) and TOKEN not in repr(error)
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__


@respx.mock
async def test_the_agent_is_shown_typing_and_a_failed_answer_is_marked():
    typing = respx.post(f"{API}/sendChatAction").mock(
        side_effect=lambda request, route: (
            httpx.Response(500) if route.call_count == 0 else ok(True)
        )
    )
    react = respx.post(f"{API}/setMessageReaction").mock(return_value=httpx.Response(500))
    respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 9}))

    async def slow(text):
        await asyncio.sleep(0.05)
        if text == "Break":
            raise RuntimeError("boom")
        return "Filed."

    async with httpx.AsyncClient() as http:
        bot = bot_with(http, Handler(slow))
        channel = bot.channels[USER]
        channel._typing_seconds = 0.02
        await channel.handle(message(1, "File this quote"))  # a refused hint breaks nothing
        with pytest.raises(RuntimeError):
            await channel.handle(message(2, "Break"))

    assert len(typing.calls) >= 3
    assert sent(typing) == {"chat_id": USER, "action": "typing"}
    assert sent(react) == {
        "chat_id": USER,
        "message_id": 2,
        "reaction": [{"type": "emoji", "emoji": "🤷"}],
    }


@respx.mock
async def test_an_answer_cut_by_a_restart_tells_the_user_to_send_it_again():
    hints_accepted()
    send = respx.post(f"{API}/sendMessage").mock(return_value=ok({"message_id": 9}))
    started = asyncio.Event()

    async def never(text):
        started.set()
        await asyncio.sleep(3600)

    async with httpx.AsyncClient() as http:
        bot = bot_with(http, Handler(never))
        handling = asyncio.create_task(
            bot.channels[USER].handle(message(3, "Wind at North Beach?"))
        )
        await started.wait()
        handling.cancel()  # the server shuts down for a deploy
        with pytest.raises(asyncio.CancelledError):
            await handling

    assert sent(send)["text"] == strings("en").chat_stopped
    assert sent(send)["reply_parameters"]["message_id"] == 3
