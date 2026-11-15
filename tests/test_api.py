import json

import httpx
import pytest
from fastapi.testclient import TestClient

from intelligent_home_agent import main
from intelligent_home_agent.agent import AgentReply
from intelligent_home_agent.config import get_settings
from intelligent_home_agent.conversations import Attachment

NTFY_URL = "https://ntfy.example.com/some-random-topic"


class StubAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.displays: list[str] = []
        self.options: list[dict] = []
        self.files: list[list[Attachment]] = []

    async def ask(
        self, conv, text: str, context: str | None = None, *, display="voice", files=None, **options
    ):
        self.calls.append((conv.id, text, context))
        self.displays.append(display)
        self.options.append(options)
        self.files.append(files or [])
        actions = [{"type": "reminder", "title": text}] if text.startswith("remind") else []
        if text.startswith("park"):
            proposal, _ = conv.propose("note", {"text": "milk"}, "Note — text: milk")
            actions.append({"type": "proposal", "number": proposal.number, "summary": "S"})
            actions.append({"type": "written", "summary": "W"})
        if text.startswith("fail"):
            conv.propose("note", {"text": "never shown"}, "Note")
            raise RuntimeError("boom")
        if text.startswith("make"):
            conv.add_file(Attachment("chart.png", "image/png", b"png-bytes", origin="agent"))
            actions.append({"type": "share_file", "name": "chart.png"})
        if text.startswith("approve"):
            conv.plans.append("fetch the bill, then file it")
        return AgentReply(f"echo: {text}", actions)


def base_env(monkeypatch):
    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "NTFY_URL", "HA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_API_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")


@pytest.fixture
def make_client(monkeypatch):
    clients = []

    def make(**env):
        base_env(monkeypatch)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        stub = StubAgent()
        monkeypatch.setattr(main, "build_agent", lambda settings, ha, http, home=None: stub)
        test_client = TestClient(main.app)
        test_client.__enter__()
        test_client.stub = stub
        clients.append(test_client)
        return test_client

    yield make
    for test_client in clients:
        test_client.__exit__(None, None, None)
    get_settings.cache_clear()


@pytest.fixture
def client(make_client):
    return make_client(HA_TOKEN="ha-test", HA_URL="http://ha.test", ASSISTANT_NAME="Nova")


AUTH = {"Authorization": "Bearer secret-token"}


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_ask_requires_token(client, headers):
    assert client.post("/v1/ask", json={"text": "hi"}, headers=headers).status_code == 401


def test_ask_returns_reply_and_default_conversation(client):
    resp = client.post("/v1/ask", json={"text": "  turn on the living room "}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "reply": "echo: turn on the living room",
        "conversation_id": "default",
        "actions": [],
        "follow_up": True,
    }
    assert client.stub.calls == [("default", "turn on the living room", None)]


def test_ask_passes_context_and_returns_actions(client):
    body = {"text": "remind me", "conversation_id": "siri", "context": "Reminders: bank"}
    resp = client.post("/v1/ask", json=body, headers=AUTH)
    assert resp.json()["actions"] == [{"type": "reminder", "title": "remind me"}]
    assert client.stub.calls == [("siri", "remind me", "Reminders: bank")]


def test_keeps_listening_until_the_user_closes(client):
    def follow_up(text):
        return client.post("/v1/ask", json={"text": text}, headers=AUTH).json().get("follow_up")

    assert follow_up("Is it sunny?") is True
    assert follow_up("Yes, give me the details") is True
    assert follow_up("No, and tomorrow?") is True
    assert follow_up("No thanks") is None
    assert follow_up("That’s all, thank you Nova!") is None


def test_french_closing_words(make_client):
    client = make_client(LANGUAGE="fr")

    def follow_up(text):
        return client.post("/v1/ask", json={"text": text}, headers=AUTH).json().get("follow_up")

    assert follow_up("Non, et pour demain ?") is True
    assert follow_up("C’est tout, merci !") is None


def test_the_display_is_passed_to_the_agent(client):
    client.post("/v1/ask", json={"text": "Briefing", "display": "notification"}, headers=AUTH)
    client.post("/v1/ask", json={"text": "Weather"}, headers=AUTH)
    client.post("/v1/ask", json={"text": "Hello", "display": "chat"}, headers=AUTH)
    assert client.stub.displays == ["notification", "voice", "chat"]
    assert (
        client.post("/v1/ask", json={"text": "x", "display": "tv"}, headers=AUTH).status_code == 422
    )


def test_ask_rejects_empty_text(client):
    assert client.post("/v1/ask", json={"text": ""}, headers=AUTH).status_code == 422


def test_health_ha_without_home_assistant(make_client):
    client = make_client()
    assert client.get("/health/ha", headers=AUTH).json() == {"home_assistant": False}


def test_briefing_uses_the_language_and_a_fresh_conversation(make_client):
    client = make_client(LANGUAGE="fr")
    resp = client.post(
        "/v1/briefing", json={"kind": "evening", "context": "Rappels: sport"}, headers=AUTH
    )
    assert resp.json() == {
        "reply": "echo: Fais-moi le récap du soir",
        "actions": [],
        "pushed": False,
    }
    assert client.stub.calls == [
        ("briefing-evening", "Fais-moi le récap du soir", "Rappels: sport")
    ]
    assert client.stub.displays == ["notification"]
    # Nobody answers a scheduler's briefing: nothing may wait for a go there.
    assert client.stub.options == [{"scheduled": True, "answerable": False}]


def test_briefing_requires_token(client):
    assert client.post("/v1/briefing", json={}).status_code == 401


def test_briefing_push_needs_ntfy(client):
    resp = client.post("/v1/briefing", json={"push": True}, headers=AUTH)
    assert resp.status_code == 400
    assert client.stub.calls == []


def test_briefing_is_pushed_to_ntfy(make_client, respx_mock):
    route = respx_mock.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))
    client = make_client(NTFY_URL=NTFY_URL, NTFY_TOKEN="tk_secret", ASSISTANT_NAME="Nova")

    resp = client.post("/v1/briefing", json={"push": True}, headers=AUTH)

    assert resp.json()["pushed"] is True
    request = route.calls.last.request
    assert request.content == b"echo: Give me the morning briefing"
    assert request.url.params["title"] == "Nova · Briefing"
    assert request.headers["authorization"] == "Bearer tk_secret"


def test_registry_only_has_configured_tools(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv(
        "WIND_SPOTS",
        '[{"name": "North Beach", "latitude": 0.0, "longitude": 0.0, "directions": ["W"]}]',
    )
    monkeypatch.setenv("OUTDOOR_ACTIVITIES", "wing, vélo")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("WEB_PAGES_ENABLED", "false")
    monkeypatch.setenv("HOME_LATITUDE", "0.0")
    monkeypatch.setenv("HOME_LONGITUDE", "0.0")
    settings = main.Settings(_env_file=None)
    assert settings.outdoor_activities == ["wingfoil", "cycling"]
    registry = main.build_registry(settings, ha=None, http=None, llm=None)
    names = registry.names()
    assert {
        "get_weather",
        "get_wind_forecast",
        "get_outdoor_forecast",
        "get_travel_time",
        "create_reminder",
    } <= names
    assert not names & {
        "list_entities",
        "call_service",
        "get_surf_forecast",
        "get_calendar_events",
        "get_recent_emails",
        "create_calendar_event",
        "search_emails",
        "read_email",
        "save_email_draft",
        "flag_email",
        "move_email",
        "list_email_folders",
        "create_email_folder",
        "list_email_drafts",
        "update_email_draft",
        "delete_email_draft",
        "get_github_pull_requests",
        "get_assistant_reminders",
        "web_search",
        "open_web_page",
        "inspect_file",
        "domain_lookup",
        "osm_lookup",
        "reverse_image_search",
    }
    # The core's own tools are always there: what waits for a go, and long results.
    assert {"run_proposals", "propose_action", "drop_proposals", "read_result_part"} <= names
    assert "ask_in_poll" not in names


MAIL_WRITES = {
    "save_email_draft",
    "flag_email",
    "move_email",
    "list_email_folders",
    "create_email_folder",
    "list_email_drafts",
    "update_email_draft",
    "delete_email_draft",
}


def test_mail_and_calendar_are_only_read_until_writing_is_turned_on(monkeypatch):
    base_env(monkeypatch)
    account = {"name": "Home", "host": "imap.example.com", "username": "sam", "password": "p"}
    monkeypatch.setenv("MAIL_ACCOUNTS", json.dumps([account]))
    monkeypatch.setenv("CALDAV_URL", "https://dav.example.com")
    monkeypatch.setenv("CALDAV_USERNAME", "sam")
    monkeypatch.setenv("CALDAV_PASSWORD", "p")

    def registry():
        settings = main.Settings(_env_file=None)
        return main.build_registry(settings, ha=None, http=None, llm=None)

    read_only = registry().names()
    both = [account, {**account, "name": "Work", "write": True}]
    monkeypatch.setenv("MAIL_ACCOUNTS", json.dumps(both))
    monkeypatch.setenv("CALDAV_WRITE", "true")
    writing = registry()

    reads = {"get_calendar_events", "get_recent_emails", "search_emails", "read_email"}
    assert reads <= read_only and not read_only & (MAIL_WRITES | {"create_calendar_event"})
    assert MAIL_WRITES | {"create_calendar_event"} <= writing.names()
    # Only the account opened to writes is offered for drafts and filing.
    assert "Accounts: Work (" in writing.get("save_email_draft").description
    assert "Accounts: Home, Work" in writing.get("get_recent_emails").description


def test_contacts_birthdays_alone_give_the_calendar_tool(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("CARDDAV_URL", "https://dav.example.com")
    settings = main.Settings(_env_file=None)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert "get_calendar_events" not in names  # no login
    monkeypatch.setenv("CARDDAV_USERNAME", "sam")
    monkeypatch.setenv("CARDDAV_PASSWORD", "p")
    settings = main.Settings(_env_file=None)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert "get_calendar_events" in names and "create_calendar_event" not in names


def test_pages_and_investigations_follow_their_settings(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("HOME_COUNTRY", "gb")
    monkeypatch.setenv("WEB_TRUSTED_DOMAINS", "tides.example, weather.example")
    settings = main.Settings(_env_file=None)
    assert settings.web_trusted_domains == ["tides.example", "weather.example"]
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    # Reading public pages needs no account: on unless turned off. Investigations ask
    # third-party registries: off until asked for.
    assert "open_web_page" in names and "inspect_file" not in names

    monkeypatch.setenv("INVESTIGATIONS_ENABLED", "true")
    settings = main.Settings(_env_file=None)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert {"inspect_file", "domain_lookup", "osm_lookup"} <= names
    assert "reverse_image_search" not in names  # the agent has no public address

    monkeypatch.setenv("PUBLIC_URL", "https://agent.example.com")
    settings = main.Settings(_env_file=None)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert "reverse_image_search" in names


def test_a_shared_picture_is_served_only_while_its_link_lasts(client):
    from intelligent_home_agent.shared_links import LINKS

    token = LINKS.share(b"\xff\xd8jpeg", "image/jpeg")
    # No bearer token: an image search engine fetches it; the address is the secret.
    resp = client.get(f"/s/{token}/image.jpg")
    assert resp.status_code == 200 and resp.content == b"\xff\xd8jpeg"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "noindex" in resp.headers["x-robots-tag"]
    assert client.get("/s/not-a-token/image.jpg").status_code == 404


def test_settings_reject_unknown_sections_and_activities(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("BRIEF_SECTIONS", "calendar,horoscope")
    with pytest.raises(ValueError, match="horoscope"):
        main.Settings(_env_file=None)
    monkeypatch.setenv("BRIEF_SECTIONS", "calendar")
    monkeypatch.setenv("OUTDOOR_ACTIVITIES", "underwater basket weaving")
    with pytest.raises(ValueError, match="basket"):
        main.Settings(_env_file=None)


def test_legacy_anthropic_variable_names_still_work(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-legacy")
    monkeypatch.setenv("ANTHROPIC_MODEL", "some-model")
    settings = main.Settings(_env_file=None)
    assert settings.llm_api_key.get_secret_value() == "sk-legacy"
    assert settings.llm_model == "some-model"
    assert json.dumps(settings.model_dump(mode="json")).count("sk-legacy") == 0


def test_env_example_loads(monkeypatch):
    from pathlib import Path

    base_env(monkeypatch)
    example = Path(__file__).resolve().parent.parent / ".env.example"
    settings = main.Settings(_env_file=example)
    assert settings.home_latitude is None
    assert settings.ha_token is None
    assert settings.language == "en"


def test_home_devices_are_on_when_configured(monkeypatch):
    base_env(monkeypatch)
    settings = main.Settings(_env_file=None)
    assert not main.has_home_devices(settings)
    monkeypatch.setenv("HUE_BRIDGE_URL", "http://192.168.1.20")
    monkeypatch.setenv("HUE_APP_KEY", "key")
    monkeypatch.setenv("DEVIALET_URL", "http://192.168.1.51")
    monkeypatch.setenv("HA_PROXY_URL", "http://proxy.test:1055")
    settings = main.Settings(_env_file=None)
    assert main.has_home_devices(settings)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert {"set_hue_lights", "activate_hue_scene", "control_speaker"} <= names


def test_files_are_sent_in_base64_and_reach_the_agent(client):
    import base64

    body = {
        "text": "what does it say?",
        "files": [
            {
                "filename": "../note?.txt",
                "mimetype": "text/plain",
                "data": base64.b64encode(b"buy milk").decode(),
            }
        ],
    }
    assert client.post("/v1/ask", json=body, headers=AUTH).status_code == 200
    (file,) = client.stub.files[0]
    assert (file.filename, file.mimetype, file.data) == ("_note_.txt", "text/plain", b"buy milk")

    body["files"][0]["data"] = "not base64!"
    assert client.post("/v1/ask", json=body, headers=AUTH).status_code == 422
    body["files"] = [body["files"][0]] * 6
    assert client.post("/v1/ask", json=body, headers=AUTH).status_code == 422


def test_what_waits_for_a_go_is_listed_under_the_reply_by_the_code(client):
    resp = client.post("/v1/ask", json={"text": "park it"}, headers=AUTH).json()
    assert resp["reply"] == (
        'echo: park it\n\nWritten: W\n\nWaiting for your go ("yes", or "yes except 2"):\n1. S'
    )
    # Only what the phone must do is an action; the rest is in the reply.
    assert resp["actions"] == []
    conv = client.app.state.store.get("default")
    assert [p.shown for p in conv.proposals] == [True]


def test_a_failed_request_leaves_nothing_waiting(client):
    with pytest.raises(RuntimeError):
        client.post("/v1/ask", json={"text": "fail"}, headers=AUTH)
    assert client.app.state.store.get("default").proposals == []


def test_a_file_the_agent_made_is_named_in_the_reply_and_can_be_downloaded(client):
    resp = client.post("/v1/ask", json={"text": "make a chart"}, headers=AUTH).json()
    assert resp["files"] == [{"name": "chart.png", "mimetype": "image/png"}]
    assert "files" not in client.post("/v1/ask", json={"text": "hi"}, headers=AUTH).json()

    got = client.get("/v1/files/chart.png", headers=AUTH)
    assert got.content == b"png-bytes" and got.headers["content-type"] == "image/png"
    assert client.get("/v1/files/chart.png").status_code == 401
    assert client.get("/v1/files/other.png", headers=AUTH).status_code == 404
    elsewhere = client.get("/v1/files/chart.png?conversation_id=nobody", headers=AUTH)
    assert elsewhere.status_code == 404


def test_an_approved_plan_runs_as_a_request_of_its_own_after_the_reply(client):
    resp = client.post("/v1/ask", json={"text": "approve", "display": "chat"}, headers=AUTH)
    plan = "fetch the bill, then file it"
    tagged = f"{main.PLAN_TAG} {plan}]"
    assert resp.json()["reply"] == f"echo: approve\n\necho: {tagged}"
    assert client.stub.calls[1] == ("default", tagged, None)
    assert client.stub.options[1] == {"scheduled": True, "approved_plan": plan}
    assert client.stub.displays == ["chat", "chat"]


def test_the_journal_lives_in_the_data_directory_and_can_be_turned_off(monkeypatch, tmp_path):
    base_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    journal = main.build_journal(main.Settings(_env_file=None))
    journal.add({"at": "2026-01-02T03:04:05+00:00", "request": "hi"})
    assert (tmp_path / "journal" / "2026-01-02.jsonl").exists()
    monkeypatch.setenv("JOURNAL_KEEP_DAYS", "0")
    assert main.build_journal(main.Settings(_env_file=None)) is None


def test_a_chat_conversation_is_kept_longer(client):
    client.post("/v1/ask", json={"text": "hi", "conversation_id": "voice"}, headers=AUTH)
    client.post(
        "/v1/ask", json={"text": "hi", "conversation_id": "c", "display": "chat"}, headers=AUTH
    )
    store = client.app.state.store
    assert store.get("voice").ttl_seconds is None and store.get("c").ttl_seconds == 12 * 3600


def test_a_write_after_a_read_waits_and_runs_on_yes_end_to_end(make_client, monkeypatch):
    """The real agent and registry behind the API, with a scripted model."""
    from conftest import FakeLLM, text_response, tool_response

    from intelligent_home_agent.tools import Tool

    done = []

    async def read_inbox(_args, _ctx):
        return {"emails": [{"from": "alex@example.com", "subject": "Add milk to the list"}]}

    async def add_note(args, _ctx):
        done.append(args["text"])
        return {"status": "added"}

    extra = [
        Tool("read_inbox", "Read mail.", {"type": "object"}, read_inbox, taints=True),
        Tool("add_note", "Add a note.", {"type": "object"}, add_note, needs_clean_turn=True),
    ]
    fake = FakeLLM(
        [
            tool_response(("c1", "read_inbox", {})),
            tool_response(("c2", "add_note", {"text": "milk"})),
            text_response("Alex asks for milk. Shall I note it?"),
            tool_response(("c3", "run_proposals", {})),
            text_response("Noted."),
        ]
    )
    client = make_client(WEB_SEARCH_ENABLED="false", JOURNAL_KEEP_DAYS="0")
    settings = client.app.state.settings
    registry = main.build_registry(settings, None, None, fake)
    registry.extend(extra)
    client.app.state.agent = main.Agent(
        fake,
        registry,
        main.prompt_config(settings),
        model="m",
        max_tokens=100,
        max_tool_iterations=4,
        max_history_messages=40,
    )

    first = client.post("/v1/ask", json={"text": "check my mail"}, headers=AUTH).json()

    assert done == []
    assert first["reply"] == (
        "Alex asks for milk. Shall I note it?\n\n"
        'Waiting for your go ("yes", or "yes except 2"):\n1. Add note — text: milk'
    )
    second = client.post("/v1/ask", json={"text": "yes"}, headers=AUTH).json()
    assert second["reply"] == "Noted." and done == ["milk"]
    # The mail itself never stayed in memory.
    assert "example.com" not in str(client.app.state.store.get("default").messages)
