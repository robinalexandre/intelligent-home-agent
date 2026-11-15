import pytest

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import build_whatsapp_chat_tools
from intelligent_home_agent.tools.base import ToolError, ToolRegistry
from intelligent_home_agent.tools.whatsapp_chats import (
    MAX_HOURS,
    MAX_MESSAGES_PER_CHAT,
    MAX_VOICE_MESSAGES,
)

NOW = 1_793_200_000
COMMUNITY = "100@g.us"
RUNNERS = "200@g.us"

GROUPS = [
    {"JID": COMMUNITY, "Name": "Kite club", "IsParent": True},
    {"JID": "101@g.us", "Name": "Kite club - Chat", "LinkedParentJID": COMMUNITY},
    {"JID": "102@g.us", "Name": "Kite club - Sessions", "LinkedParentJID": COMMUNITY},
    {"JID": RUNNERS, "Name": "Friday runners", "IsParent": False, "LinkedParentJID": ""},
    {"JID": "300@g.us", "Name": "Neighbours", "IsParent": False, "LinkedParentJID": ""},
]


def message(at, body, name="Jo", from_me=False, **extra):
    return {
        "timestamp": at,
        "body": body,
        "fromMe": from_me,
        "hasMedia": False,
        "_data": {"Info": {"PushName": name}},
        **extra,
    }


class FakeWaha:
    def __init__(self, messages, unread=None):
        self.by_chat = messages
        self.unread = unread or {}
        self.asked = []
        self.marked = []

    async def groups(self):
        return GROUPS

    async def messages(self, chat_id, since, limit):
        self.asked.append((chat_id, since, limit))
        return self.by_chat.get(chat_id, [])

    async def mark_read(self, chat_id, messages, days):
        self.marked.append((chat_id, messages, days))
        return self.unread.get(chat_id, [])


def tools_for(waha, allowed=(COMMUNITY, RUNNERS)):
    return build_whatsapp_chat_tools(waha, list(allowed), "UTC", clock=lambda: NOW)


def tool_for(waha, allowed=(COMMUNITY, RUNNERS)):
    return tools_for(waha, allowed)[0]


def mark_tool(waha, allowed=(COMMUNITY, RUNNERS)):
    return tools_for(waha, allowed)[1]


async def test_reads_a_group_by_part_of_its_name_oldest_first(ctx):
    waha = FakeWaha(
        {
            RUNNERS: [
                message(NOW - 60, "Same here", replyTo={"body": "No surprise!"}),
                message(NOW - 600, "North Beach on Saturday?", name="Sam"),
                message(NOW - 300, "Definitely", from_me=True),
            ]
        }
    )
    tool = tool_for(waha)

    result = await tool.handler({"chat": "runners"}, ctx)

    assert tool.taints
    (chat,) = result["chats"]
    assert chat["chat"] == "Friday runners"
    assert [(m["from"], m["text"]) for m in chat["messages"]] == [
        ("Sam", "North Beach on Saturday?"),
        ("(the user)", "Definitely"),
        ("Jo", "Same here"),
    ]
    assert chat["messages"][2]["replies_to"] == "No surprise!"
    assert waha.asked == [(RUNNERS, NOW - 24 * 3600, MAX_MESSAGES_PER_CHAT)]


async def test_voice_messages_of_a_group_are_transcribed_newest_first(ctx):
    def voice(at, mid):
        return message(
            at,
            "",
            id=mid,
            hasMedia=True,
            _data={
                "Info": {"PushName": "Sam", "MediaType": "ptt"},
                "Message": {"audioMessage": {}},
            },
        )

    voices = [voice(NOW - 60 * n, f"v{n}") for n in range(1, MAX_VOICE_MESSAGES + 3)]
    waha = FakeWaha({RUNNERS: voices})
    downloaded = []

    async def download(chat_id, message_id):
        downloaded.append((chat_id, message_id))
        return Attachment("voice.oga", "audio/ogg", message_id.encode())

    async def transcribe(audio):
        return f"heard {audio.decode()}"

    waha.download = download
    tool = build_whatsapp_chat_tools(
        waha, [RUNNERS], "UTC", clock=lambda: NOW, transcribe=transcribe
    )[0]

    result = await tool.handler({"chat": "runners"}, ctx)

    said = [m.get("said") for m in result["chats"][0]["messages"]]
    assert said[-1] == "heard v1"
    assert said[:2] == [None, None]
    assert len(downloaded) == MAX_VOICE_MESSAGES
    assert tool.taints


async def test_a_community_brings_all_its_groups_and_nothing_else(ctx):
    waha = FakeWaha({"101@g.us": [message(NOW - 10, "Session tomorrow")]})

    result = await tool_for(waha).handler({"chat": "kite"}, ctx)

    assert [c["chat"] for c in result["chats"]] == ["Kite club - Chat"]
    assert result["chats"][0]["community"] == "Kite club"
    assert result["quiet"] == ["Kite club - Sessions"]
    assert {chat for chat, _, _ in waha.asked} == {"101@g.us", "102@g.us"}
    assert result["readable"] == ["Friday runners", "Kite club"]


async def test_never_reads_a_chat_that_was_not_allowed(ctx):
    waha = FakeWaha({"300@g.us": [message(NOW - 10, "Lunch on Sunday")]})
    tool = tool_for(waha)

    result = await tool.handler({"chat": "neighbours"}, ctx)
    everything = await tool.handler({}, ctx)

    assert result["chats"] == []
    assert result["readable"] == ["Friday runners", "Kite club"]
    assert "300@g.us" not in {chat for chat, _, _ in waha.asked}
    assert everything["chats"] == []


async def test_hours_are_capped_at_a_week_and_empty_messages_skipped(ctx):
    waha = FakeWaha(
        {
            RUNNERS: [
                message(NOW - 10, ""),
                message(NOW - 20, "", hasMedia=True, _data={"Info": {"MediaType": "image"}}),
            ]
        }
    )

    result = await tool_for(waha, [RUNNERS]).handler({"hours": 10_000}, ctx)

    assert waha.asked[0][1] == NOW - MAX_HOURS * 3600
    (chat,) = result["chats"]
    assert chat["messages"] == [
        {"at": chat["messages"][0]["at"], "from": "someone", "text": "", "file": "image"}
    ]


async def test_unread_keeps_what_came_after_the_last_message_read_or_written(ctx):
    waha = FakeWaha(
        {
            RUNNERS: [
                message(NOW - 10, "Who is coming?", ack=2),
                message(NOW - 20, "Session at 6pm", ack=2),
                message(NOW - 30, "Read on the phone", ack=3),
                message(NOW - 40, "Missed by the receipt", ack=2),
            ],
            "101@g.us": [
                message(NOW - 10, "After my answer", ack=2),
                message(NOW - 20, "My answer", from_me=True, ack=0),
            ],
            "102@g.us": [message(NOW - 10, "All read", ack=4)],
        }
    )
    tool = tool_for(waha)

    result = await tool.handler({"unread": True}, ctx)

    assert waha.asked[0][1] == NOW - MAX_HOURS * 3600
    assert {c["chat"]: [m["text"] for m in c["messages"]] for c in result["chats"]} == {
        "Kite club - Chat": ["After my answer"],
        "Friday runners": ["Session at 6pm", "Who is coming?"],
    }
    assert result["quiet"] == ["Kite club - Sessions"]


async def test_a_long_chat_keeps_the_newest_messages(ctx):
    long = "x" * 1000
    waha = FakeWaha({RUNNERS: [message(NOW - i, f"{i} {long}") for i in range(100)]})

    result = await tool_for(waha, [RUNNERS]).handler({}, ctx)

    (chat,) = result["chats"]
    assert chat["older_left_out"]
    assert 0 < len(chat["messages"]) < 100
    assert chat["messages"][-1]["text"].startswith("0 ")


async def test_marks_as_read_the_groups_of_a_community_and_nothing_else(ctx):
    waha = FakeWaha({}, unread={"101@g.us": ["false_101@g.us_A", "false_101@g.us_B"]})
    tool = mark_tool(waha)

    result = await tool.handler({"chat": "kite"}, ctx)

    assert waha.marked == [
        ("101@g.us", MAX_MESSAGES_PER_CHAT, MAX_HOURS // 24),
        ("102@g.us", MAX_MESSAGES_PER_CHAT, MAX_HOURS // 24),
    ]
    assert result == {
        "marked_read": [{"chat": "Kite club - Chat", "messages": 2}],
        "nothing_unread": ["Kite club - Sessions"],
    }
    assert await tool.summary({"chat": "kite"}) == (
        "Mark as read on WhatsApp the messages of: Kite club - Chat, Kite club - Sessions"
    )
    french = build_whatsapp_chat_tools(waha, [COMMUNITY], "UTC", language="fr")[1]
    assert (await french.summary({"chat": "kite"})).startswith("Marquer comme lus sur WhatsApp")


async def test_never_marks_a_chat_that_was_not_allowed(ctx):
    waha = FakeWaha({})
    tool = mark_tool(waha)

    with pytest.raises(ToolError, match="Readable: Friday runners, Kite club"):
        await tool.handler({"chat": "neighbours"}, ctx)
    await tool.handler({}, ctx)

    assert {chat for chat, _, _ in waha.marked} == {"101@g.us", "102@g.us", RUNNERS}


async def test_marking_read_tells_what_came_after_the_summary(ctx):
    now = [NOW]
    waha = FakeWaha(
        {RUNNERS: [message(NOW - 60, "Already summed up")]}, unread={RUNNERS: ["A", "B", "C"]}
    )
    read, mark = build_whatsapp_chat_tools(waha, [RUNNERS], "UTC", clock=lambda: now[0])

    await read.handler({"unread": True}, ctx)
    now[0] = NOW + 600
    waha.by_chat[RUNNERS] += [
        message(NOW + 300, "Came in between", name="Sam"),
        message(NOW + 400, "My answer", from_me=True),
    ]
    ctx.tainted = False
    first = await mark.handler({"chat": "runners"}, ctx)
    again = await mark.handler({"chat": "runners"}, ctx)

    (later,) = first["came_after_the_summary"]
    assert later["chat"] == "Friday runners"
    assert [(m["from"], m["text"]) for m in later["messages"]] == [("Sam", "Came in between")]
    assert ctx.tainted
    assert "came_after_the_summary" not in again


async def test_marking_read_after_a_summary_waits_for_the_users_go(ctx):
    waha = FakeWaha({})
    registry = ToolRegistry(tools_for(waha))
    ctx.tainted = True

    parked = await registry.run("mark_whatsapp_chats_read", {"chat": "runners"}, ctx)
    done = await registry.run("mark_whatsapp_chats_read", {"chat": "runners"}, ctx, approved=True)

    assert parked["status"] == "waiting_for_the_user"
    assert ctx.actions[0]["summary"].endswith(": Friday runners")
    assert waha.marked == [(RUNNERS, MAX_MESSAGES_PER_CHAT, MAX_HOURS // 24)]
    assert done == {"marked_read": [], "nothing_unread": ["Friday runners"]}


def test_a_summary_to_mark_read_is_of_the_unread_messages():
    read = tool_for(object())

    assert 'mark as read") is of those: unread true' in read.description
