"""How the chat channels are set up from the settings, and reached through the API."""

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from intelligent_home_agent import main
from intelligent_home_agent.agent import AgentReply
from intelligent_home_agent.config import Settings, get_settings
from intelligent_home_agent.tools import ToolRegistry, build_proposal_tools

AUTH = {"Authorization": "Bearer secret-token"}
CHAT = "15550100@c.us"
TOKEN = "123456:test-token"
WHATSAPP = {
    "WAHA_URL": "http://waha.test",
    "WAHA_API_KEY": "user-key",
    "WAHA_SESSION": "user",
    "WHATSAPP_CHAT_ID": CHAT,
}
TELEGRAM = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_IDS": "4242, 77"}
CHAT_SETTINGS = (*WHATSAPP, *TELEGRAM, "AGENT_WAHA_SESSION", "AGENT_WAHA_URL", "AGENT_WAHA_API_KEY")


class StubAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict]] = []
        self.tools = ToolRegistry()
        self.tools.extend(build_proposal_tools(self.tools, polls=True))

    async def ask(self, conv, text, context=None, **options):
        self.calls.append((conv.id, text, context, options))
        actions = []
        if "park" in text:
            proposal, _ = conv.propose("note", {"text": "milk"}, "Note — text: milk")
            actions.append({"type": "proposal", "number": proposal.number, "summary": "S"})
        return AgentReply(f"echo: {text}", actions)


def settings_with(monkeypatch, **env):
    for name in (*CHAT_SETTINGS, "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "HA_TOKEN", "WHISPER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_API_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


@pytest.fixture
def make_client(monkeypatch):
    clients = []

    async def no_events(self):
        # WAHA's event socket is not HTTP: left out, so no test opens a connection.
        await asyncio.sleep(3600)
        yield

    def make(**env):
        settings_with(monkeypatch, **env)
        monkeypatch.setattr(main.Waha, "events", no_events)
        get_settings.cache_clear()
        stub = StubAgent()
        monkeypatch.setattr(main, "build_agent", lambda settings, ha, http, home=None: stub)
        client = TestClient(main.app)
        client.__enter__()
        client.stub = stub
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
    get_settings.cache_clear()


def waha_quiet():
    respx.get(url__startswith="http://waha.test/api/user/chats/").mock(
        return_value=httpx.Response(200, json=[])
    )


def test_no_channel_exists_until_one_is_configured(monkeypatch):
    settings = settings_with(monkeypatch)

    assert not settings.chat_enabled
    built = main.build_channels(settings, http=None)
    assert built.channels == [] and built.loops == []
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert not names & {"ask_in_poll", "read_whatsapp_chats", "mark_whatsapp_chats_read"}
    # Half a configuration is none: a token without a chat would answer nobody, or anybody.
    assert not settings_with(monkeypatch, TELEGRAM_BOT_TOKEN=TOKEN).chat_enabled
    assert not settings_with(
        monkeypatch, WAHA_URL="http://waha.test", WAHA_API_KEY="k"
    ).chat_enabled


async def test_both_channels_can_run_at_once(monkeypatch):
    settings = settings_with(monkeypatch, **WHATSAPP, **TELEGRAM)
    assert settings.telegram_chat_ids == [4242, 77]

    async with httpx.AsyncClient() as http:
        built = main.build_channels(settings, http)
        for loop in built.loops:
            loop.close()  # never started here

    assert [c.name for c in built.channels] == ["whatsapp", "telegram", "telegram-77"]
    # WhatsApp: its messages and its votes; Telegram: the bot, then two per chat.
    assert len(built.loops) == 2 + 1 + 2 * 2
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert "ask_in_poll" in names and "read_whatsapp_chats" not in names


def test_the_chat_runs_on_the_agents_own_session_and_reads_from_the_users(monkeypatch):
    env = {**WHATSAPP, "WHATSAPP_READABLE_CHATS": "200@g.us, 300@g.us"}
    settings = settings_with(monkeypatch, **env)
    assert settings.chat_waha == ("http://waha.test", "user-key", "user")
    assert not settings.whatsapp_own_number
    assert settings.whatsapp_readable_chats == ["200@g.us", "300@g.us"]

    env |= {"AGENT_WAHA_SESSION": "agent", "AGENT_WAHA_API_KEY": "agent-key"}
    settings = settings_with(monkeypatch, **env)
    assert settings.chat_waha == ("http://waha.test", "agent-key", "agent")
    assert settings.whatsapp_own_number and settings.whatsapp_reading_enabled

    for key in ("WAHA_URL", "WAHA_API_KEY"):
        env.pop(key)
    settings = settings_with(monkeypatch, **env, AGENT_WAHA_URL="http://agent.test")
    assert settings.whatsapp_enabled and not settings.whatsapp_reading_enabled


async def test_other_chats_are_read_only_when_some_are_listed(monkeypatch):
    settings = settings_with(monkeypatch, **WHATSAPP, WHATSAPP_READABLE_CHATS="200@g.us")

    async with httpx.AsyncClient() as http:
        registry = main.build_registry(settings, ha=None, http=http, llm=None)

    assert {"read_whatsapp_chats", "mark_whatsapp_chats_read"} <= registry.names()
    assert registry.get("read_whatsapp_chats").taints
    assert registry.get("mark_whatsapp_chats_read").needs_clean_turn


def test_voice_messages_need_a_model_and_the_extra(monkeypatch, tmp_path, caplog):
    assert main.build_transcriber(settings_with(monkeypatch)) is None

    settings = settings_with(monkeypatch, WHISPER_MODEL="tiny", LANGUAGE="fr")
    monkeypatch.setattr(main, "voice_available", lambda: False)
    assert main.build_transcriber(settings) is None
    assert 'pip install ".[voice]"' in caplog.text

    monkeypatch.setattr(main, "voice_available", lambda: True)
    transcriber = main.build_transcriber(settings)
    # The user's language unless they listed others; the model lives in the data folder.
    assert transcriber._languages == ["fr"]
    assert transcriber._download_dir == str(tmp_path / "agent-data" / "whisper")


def test_delivering_in_a_chat_needs_a_channel(make_client):
    client = make_client()

    for path, body in (
        ("/v1/ask", {"text": "Hello", "deliver": "chat"}),
        ("/v1/briefing", {"deliver": "telegram"}),
    ):
        assert client.post(path, json=body, headers=AUTH).status_code == 503
    assert client.stub.calls == []


@respx.mock
def test_a_request_delivered_in_the_chat_goes_on_there(make_client, data_dir):
    waha_quiet()
    send = respx.post("http://waha.test/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    poll = respx.post("http://waha.test/api/sendPoll").mock(
        return_value=httpx.Response(201, json={"id": "P1"})
    )
    respx.put("http://waha.test/api/reaction").mock(return_value=httpx.Response(200, json={}))
    client = make_client(**WHATSAPP)
    assert list(client.app.state.chats) == ["whatsapp"]

    body = {
        "text": "park the note",
        "conversation_id": "siri",
        "context": "Reminders: gym",
        "display": "notification",
        "deliver": "chat",
    }
    resp = client.post("/v1/ask", json=body, headers=AUTH).json()

    assert resp["conversation_id"] == "chat-whatsapp"
    assert resp["reply"].startswith("echo: park the note") and "pick below" in resp["reply"]
    conv_id, text, context, options = client.stub.calls[0]
    assert (conv_id, text, context) == ("chat-whatsapp", "park the note", "Reminders: gym")
    assert options["display"] == "chat" and not options["scheduled"]
    sent = [httpx.Response(200, content=c.request.content).json() for c in send.calls]
    assert sent == [{"session": "user", "chatId": CHAT, "text": f"🤖 {resp['reply']}"}]
    # What waits for the go is offered right away, as a poll, and kept across restarts.
    options = httpx.Response(200, content=poll.calls[0].request.content).json()["poll"]["options"]
    assert options == ["Note — text: milk", "✅ Go", "❌ Cancel"]
    assert len(list((data_dir / "conversations").glob("*.json"))) == 1
    conv = client.app.state.chats["whatsapp"].conversation()
    assert conv.kept and conv.proposals[0].poll_id == "P1"


@respx.mock
def test_a_briefing_delivered_in_the_chat_can_be_answered_there(make_client):
    waha_quiet()
    send = respx.post("http://waha.test/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    client = make_client(**WHATSAPP, WHATSAPP_MARKER="🌿")

    resp = client.post(
        "/v1/briefing", json={"kind": "evening", "deliver": "whatsapp"}, headers=AUTH
    ).json()

    assert resp == {
        "reply": "echo: Give me the evening recap",
        "actions": [],
        "pushed": False,
        "delivered": True,
    }
    conv_id, _, _, options = client.stub.calls[0]
    # In the chat's own conversation, as a run the user did not just ask for.
    assert conv_id == "chat-whatsapp" and options["scheduled"] is True
    assert "🌿 echo: Give me the evening recap" in send.calls[0].request.content.decode()


@respx.mock
def test_the_agent_starts_and_stops_with_a_telegram_channel(make_client):
    respx.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    client = make_client(**TELEGRAM)

    assert list(client.app.state.chats) == ["telegram", "telegram-77"]
    assert client.get("/health").status_code == 200


@respx.mock
def test_what_the_agent_says_by_itself_goes_to_the_chat_first(make_client, monkeypatch):
    waha_quiet()
    send = respx.post("http://waha.test/api/sendText").mock(
        return_value=httpx.Response(201, json={"id": "out1"})
    )
    poll = respx.post("http://waha.test/api/sendPoll").mock(
        return_value=httpx.Response(201, json={"id": "P1"})
    )
    respx.put("http://waha.test/api/reaction").mock(return_value=httpx.Response(200, json={}))
    ntfy = respx.post("https://ntfy.example.com/topic").mock(return_value=httpx.Response(200))
    client = make_client(**WHATSAPP, NTFY_URL="https://ntfy.example.com/topic")

    delivery = client.app.state.delivery
    assert [c.name for c in delivery.channels] == ["whatsapp", "ntfy"]
    assert delivery.channel.display == "chat"
    assert delivery.channel.conversation_id == "chat-whatsapp"

    client.portal.call(main.run_task, client.app, "Bills", "park the note")

    conv_id, text, _, options = client.stub.calls[0]
    # The task runs in the chat's conversation, so what it parks can be approved there.
    assert conv_id == "chat-whatsapp"
    assert text.startswith("[Scheduled task the user left you: Bills]")
    assert options["display"] == "chat" and options["scheduled"] is True
    (said,) = [httpx.Response(200, content=c.request.content).json()["text"] for c in send.calls]
    assert said.startswith("🤖 ⏰ Bills\n\necho: [Scheduled task") and "pick below" in said
    options = httpx.Response(200, content=poll.calls[0].request.content).json()["poll"]["options"]
    assert options[0] == "Note — text: milk"
    assert not ntfy.called

    # A chat that can't be reached leaves the report to the push notification.
    send.mock(return_value=httpx.Response(502))
    client.portal.call(main.run_task, client.app, "Bills", "again")
    assert ntfy.call_count == 1
    client.portal.call(main.tell_user, client.app, "Pets", "The bin is full.")
    assert ntfy.call_count == 2


def test_a_chat_alone_turns_the_tasks_on(monkeypatch):
    assert main.reports_reach(settings_with(monkeypatch)) is None
    reach = main.reports_reach(settings_with(monkeypatch, **TELEGRAM))
    assert reach == "a message in the chat they talk to you in"
    assert main.reports_reach(settings_with(monkeypatch, **TELEGRAM, TASKS_STORE="off")) is None
