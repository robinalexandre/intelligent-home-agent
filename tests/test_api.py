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
        "delivered": False,
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
        "create_assistant_reminder",
        "create_handoff",
        "close_assistant_reminder",
        "list_repositories",
        "browse_repository",
        "check_dependencies",
        "schedule_task",
        "list_scheduled_tasks",
        "web_search",
        "find_exercise_photos",
        "generate_image",
        "generate_video",
        "run_code",
        "open_web_page",
        "inspect_file",
        "domain_lookup",
        "osm_lookup",
        "reverse_image_search",
        "ask_in_poll",
        "read_whatsapp_chats",
        "mark_whatsapp_chats_read",
        "list_drive_folder",
        "search_drive_files",
        "read_drive_file",
        "save_file_to_drive",
        "delete_drive_file",
        "list_drive_trash",
        "search_drive_content",
        "get_reminders",
        "update_reminder",
        "complete_reminder",
        "search_apple_notes",
        "read_apple_note",
        "find_my_devices",
        "play_sound_on_device",
        "request_login",
        "browser_open",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    }
    assert registry.jobs == []
    # What it keeps works out of the box, in local files: memory, how-tos, alerts turned
    # down, and the journal's search.
    kept = {
        "remember",
        "forget",
        "find_procedure",
        "save_procedure",
        "turn_down_alert",
        "list_turned_down_alerts",
        "bring_back_alert",
        "read_day_journal",
        "search_past_conversations",
    }
    assert kept <= names
    monkeypatch.setenv("MEMORY_STORE", "off")
    monkeypatch.setenv("JOURNAL_KEEP_DAYS", "0")
    off = main.build_registry(main.Settings(_env_file=None), ha=None, http=None, llm=None)
    assert off.names() & kept == {
        "turn_down_alert",
        "list_turned_down_alerts",
        "bring_back_alert",
    }
    # Documents need no account: on with what the documents extra brings.
    assert {"list_saved_files", "read_saved_file", "create_pdf", "fill_pdf"} <= names
    assert {"read_pdf_layout", "copy_pdf_pages", "draw_image", "make_animation"} <= names
    # The core's own tools are always there: what waits for a go, and long results.
    assert {"run_proposals", "propose_action", "drop_proposals", "read_result_part"} <= names
    assert "ask_in_poll" not in names


def test_documents_pictures_and_the_sandbox_follow_their_settings(monkeypatch):
    class Sandboxed:
        files = object()

    base_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("HOME_COUNTRY", "US")
    monkeypatch.setenv("EXERCISE_PHOTOS_ENABLED", "true")
    monkeypatch.setenv("CODE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("GENERATION_OPENAI_BASE_URL", "http://192.168.1.50:8080/v1")
    monkeypatch.setenv("GENERATION_OPENAI_VIDEO_MODEL", "off")
    settings = main.Settings(_env_file=None)
    registry = main.build_registry(settings, ha=None, http=object(), llm=Sandboxed())
    names = registry.names()
    assert {"find_exercise_photos", "save_exercise_photos", "generate_image", "run_code"} <= names
    assert "generate_video" not in names  # that server makes no video
    described = {d["name"]: d["description"] for d in registry.definitions()}
    assert "US Letter paper" in described["create_pdf"]  # PAPER_SIZE=auto, from the country

    # The sandbox is Anthropic's: with another provider, or an SDK without files, no tool.
    assert "run_code" not in main.build_registry(settings, ha=None, http=None, llm=None).names()
    monkeypatch.setenv("LLM_PROVIDER", "mistral")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("GENERATION_GEMINI_API_KEY", "gkey")
    monkeypatch.setenv("GENERATION_DEFAULT_PROVIDER", "gemini")
    monkeypatch.setenv("DOCUMENTS_ENABLED", "false")
    monkeypatch.setenv("PAPER_SIZE", "a4")
    settings = main.Settings(_env_file=None)
    registry = main.build_registry(settings, ha=None, http=None, llm=Sandboxed())
    names = registry.names()
    assert "run_code" not in names and {"generate_image", "generate_video"} <= names
    assert not names & {"create_pdf", "list_saved_files", "draw_image", "find_exercise_photos"}
    schema = {d["name"]: d["input_schema"] for d in registry.definitions()}
    assert schema["generate_image"]["properties"]["provider"]["enum"] == ["gemini", "openai"]
    assert schema["generate_video"]["properties"]["provider"]["enum"] == ["gemini"]


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


# --- What the agent keeps: memory, and chats across restarts --------------------------------


def settings_with(monkeypatch, **env):
    base_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return main.Settings(_env_file=None)


async def test_memory_is_local_files_in_the_data_directory_by_default(monkeypatch, data_dir):
    settings = settings_with(monkeypatch, ASSISTANT_NAME="Nova")
    assert main.memory_paths(settings) == (
        str(data_dir / "memory.md"),
        str(data_dir / "procedures.md"),
    )
    kept = main.build_kept(settings, http=None)
    await kept.memory.remember("My accountant is Jo Martin")
    kept.turned_down.add("Late parcel from the bookshop")
    assert (data_dir / "memory.md").read_text().startswith("# Nova — long-term memory")
    # Both reach every request's prompt, through the agent's memory hook.
    text = await kept.prompt_text()
    assert text.startswith("- My accountant is Jo Martin (")
    assert "Alerts the user turned down" in text and "- Late parcel from the bookshop" in text
    off = main.build_kept(settings_with(monkeypatch, MEMORY_STORE="off"), http=None)
    assert off.memory is None and off.procedures is None


def test_the_memory_reaches_the_prompt_of_the_real_agent(make_client, monkeypatch):
    from conftest import FakeLLM, text_response, tool_response

    fake = FakeLLM(
        [
            tool_response(("c1", "remember", {"fact": "I take my coffee black"})),
            text_response("Noted."),
            text_response("Black."),
        ]
    )
    monkeypatch.setattr(main, "build_llm_client", lambda *args: fake)
    build_agent = main.build_agent  # the real one: make_client puts a stub in its place
    client = make_client(WEB_SEARCH_ENABLED="false", USER_NAME="Sam")
    client.app.state.agent = build_agent(client.app.state.settings, None, None)

    client.post("/v1/ask", json={"text": "remember I take my coffee black"}, headers=AUTH)
    client.post("/v1/reset", headers=AUTH)
    reply = client.post("/v1/ask", json={"text": "how do I take my coffee?"}, headers=AUTH)

    assert reply.json()["reply"] == "Black."
    system = str(fake.requests[-1]["system"])
    assert "What you remember about the user" in system
    assert "- I take my coffee black (" in system and "The user is called Sam" in system
    assert "What you remember" not in str(fake.requests[0]["system"])


def test_memory_in_a_repository_lives_in_a_folder_named_after_the_assistant(monkeypatch):
    with pytest.raises(ValueError, match="MEMORY_REPO"):
        settings_with(monkeypatch, MEMORY_STORE="github")
    settings = settings_with(
        monkeypatch,
        MEMORY_STORE="github",
        GITHUB_TOKEN="t",
        MEMORY_REPO="me/config",
        ASSISTANT_NAME="Mrs. Hudson",
    )
    assert main.memory_paths(settings) == ("mrs-hudson/memory.md", "mrs-hudson/procedures.md")
    kept = main.build_kept(settings, http=None)
    assert kept.memory is not None and kept.procedures is not None
    assert main.build_crypt(settings) is None
    monkeypatch.setenv("MEMORY_PATH", "/notes/agent.md")
    assert main.memory_paths(main.Settings(_env_file=None))[0] == "notes/agent.md"


@pytest.fixture
def crypt_settings(monkeypatch):
    from test_config_crypt import KEY

    def make(**env):
        main._crypt_for.cache_clear()
        return settings_with(
            monkeypatch,
            **{
                "MEMORY_STORE": "github",
                "GITHUB_TOKEN": "t",
                "MEMORY_REPO": "me/config",
                "ASSISTANT_NAME": "Nova",
                "MEMORY_CRYPT_KEY": KEY,
                **env,
            },
        )

    yield make
    main._crypt_for.cache_clear()


ATTRIBUTES_URL = "https://api.github.com/repos/me/config/contents/.gitattributes"


async def test_the_encrypted_folders_come_from_the_repository(crypt_settings, respx_mock):
    settings = crypt_settings()
    crypt = main.build_crypt(settings)
    # Before the repository was read, the agent's own folder counts as encrypted.
    assert crypt.patterns == ["nova/**"] and crypt.encrypts("nova/memory.md")
    assert main.build_crypt(settings) is crypt  # one codec for the memory and the procedures
    route = respx_mock.get(ATTRIBUTES_URL).respond(
        text="secret/** filter=git-crypt diff=git-crypt\n"
    )
    async with httpx.AsyncClient() as http:
        await main.load_crypt_patterns(settings, http)
    assert route.called and crypt.patterns == ["secret/**"]
    assert not crypt.encrypts("nova/memory.md")


async def test_an_unreadable_gitattributes_keeps_the_agents_files_encrypted(
    crypt_settings, respx_mock
):
    settings = crypt_settings(MEMORY_PATH="memory.md")
    respx_mock.get(ATTRIBUTES_URL).respond(404)
    async with httpx.AsyncClient() as http:
        await main.load_crypt_patterns(settings, http)
    crypt = main.build_crypt(settings)
    assert crypt.encrypts("memory.md") and crypt.encrypts("nova/procedures.md")


def test_a_key_that_cannot_be_used_turns_the_memory_off_rather_than_write_in_clear(
    crypt_settings, monkeypatch
):
    kept = main.build_kept(crypt_settings(MEMORY_CRYPT_KEY="bm90IGEga2V5"), http=None)
    assert kept.memory is None and kept.procedures is None
    # The same without the optional cryptography package.
    settings = crypt_settings()
    monkeypatch.setattr(main, "crypt_available", lambda: False)
    kept = main.build_kept(settings, http=None)
    assert kept.memory is None and kept.turned_down is not None


def chat(client, text, conversation_id="c", **extra):
    body = {"text": text, "conversation_id": conversation_id, "display": "chat", **extra}
    return client.post("/v1/ask", json=body, headers=AUTH)


def test_a_chat_survives_a_restart_with_its_files_and_what_waits(make_client, data_dir):
    first = make_client()
    chat(first, "park it")
    chat(first, "make a chart")
    first.app.state.store.get("c").messages.append({"role": "user", "content": "park it"})
    chat(first, "and then?")
    first.post("/v1/ask", json={"text": "hi", "conversation_id": "voice"}, headers=AUTH)

    # Nothing on disk is named after what the client sent.
    (saved,) = (data_dir / "conversations").glob("*.json")
    assert len(saved.stem) == 32 and saved.stat().st_mode & 0o777 == 0o600

    restarted = make_client()
    store = restarted.app.state.store
    assert store.peek("voice") is None  # a spoken exchange is over in minutes: memory only
    conv = store.peek("c")
    assert conv is not None and conv.ttl_seconds == 12 * 3600
    assert conv.messages == [{"role": "user", "content": "park it"}]
    assert [(p.number, p.tool, p.args, p.shown) for p in conv.proposals] == [
        (1, "note", {"text": "milk"}, True)
    ]
    assert conv.files["chart.png"].data == b"png-bytes"
    assert conv.files["chart.png"].origin == "agent"
    download = restarted.get("/v1/files/chart.png?conversation_id=c", headers=AUTH)
    assert download.content == b"png-bytes"
    # The next request goes on in the same conversation and is saved, whatever its display.
    before = saved.read_text()
    restarted.post("/v1/ask", json={"text": "yes", "conversation_id": "c"}, headers=AUTH)
    assert saved.read_text() != before and json.loads(saved.read_text())["id"] == "c"


def test_a_kept_file_is_stored_under_a_hash_never_under_its_name(make_client, data_dir):
    client = make_client()
    chat(client, "make a chart")
    (directory,) = (data_dir / "files").iterdir()
    names = {p.name for p in directory.iterdir()}
    assert "index.json" in names and "chart.png" not in names
    assert json.loads((directory / "index.json").read_text())["chart.png"]["origin"] == "agent"


def test_reset_forgets_the_saved_conversation_and_its_files(make_client, data_dir):
    client = make_client()
    chat(client, "make a chart")
    assert client.post("/v1/reset?conversation_id=c", headers=AUTH).status_code == 204
    assert list((data_dir / "conversations").glob("*.json")) == []
    assert list((data_dir / "files").iterdir()) == []
    assert make_client().app.state.store.peek("c") is None


def test_expired_or_damaged_saved_conversations_are_removed_at_startup(make_client, data_dir):
    client = make_client(CHAT_CONVERSATION_TTL_SECONDS="60")
    chat(client, "make a chart", "old")
    chat(client, "hi", "bad")
    old = main.conversation_file(client.app.state.settings, "old")
    data = json.loads(old.read_text())
    old.write_text(json.dumps({**data, "saved_at": data["saved_at"] - 3600}))
    main.conversation_file(client.app.state.settings, "bad").write_text('{"id": "bad"}')

    restarted = make_client(CHAT_CONVERSATION_TTL_SECONDS="60")

    assert restarted.app.state.store.peek("old") is None
    assert restarted.app.state.store.peek("bad") is None
    assert list((data_dir / "conversations").glob("*.json")) == []
    # The files outlive their conversation, for their days: the same chat finds them again.
    chat(restarted, "hi", "old")
    assert "chart.png" in restarted.app.state.store.peek("old").files


def test_files_can_stay_in_memory_only(make_client, data_dir):
    client = make_client()
    chat(client, "make a chart")
    restarted = make_client(FILES_KEEP_DAYS="0")
    conv = restarted.app.state.store.peek("c")
    assert conv is not None and "chart.png" not in conv.files
    assert list((data_dir / "files").iterdir()) == []


def test_what_the_tools_do_in_the_background_runs_while_the_app_does(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    base_env(monkeypatch)
    get_settings.cache_clear()
    started, stopped = [], []

    async def job():
        started.append(1)
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.append(1)

    stub = StubAgent()
    stub.tools = SimpleNamespace(jobs=[job])
    monkeypatch.setattr(main, "build_agent", lambda settings, ha, http, home=None: stub)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert started == [1] and stopped == []
    assert stopped == [1]


def test_home_and_health_tools_come_with_their_settings(monkeypatch, tmp_path):
    base_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    names = main.build_registry(main.Settings(_env_file=None), None, None, None).names()
    off = {
        "get_lock_state",
        "unlock_door",
        "get_home_security_status",
        "get_pet_devices_status",
        "get_tv_state",
        "get_apple_media_state",
        "get_body_measurements",
        "note_delivery",
        "list_deliveries",
    }
    assert not names & off
    monkeypatch.setenv("NUKI_API_TOKEN", "n")
    monkeypatch.setenv("NETATMO_CLIENT_ID", "c")
    monkeypatch.setenv("NETATMO_CLIENT_SECRET", "s")
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "c")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "s")
    monkeypatch.setenv(
        "MAIL_ACCOUNTS",
        '[{"name": "home", "host": "imap.example.com", "username": "u", "password": "p"}]',
    )
    registry = main.build_registry(main.Settings(_env_file=None), None, None, None)
    assert {
        "get_lock_state",
        "unlock_door",
        "get_home_security_status",
        "get_body_measurements",
        "note_delivery",
        "list_deliveries",
    } <= registry.names()
    assert registry.devices == {}
    monkeypatch.setenv("DELIVERIES_ENABLED", "false")
    names = main.build_registry(main.Settings(_env_file=None), None, None, None).names()
    assert "note_delivery" not in names


def test_the_home_network_client_reaches_the_devices_whatever_the_home_coordinates(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("HOME_LATITUDE", "0.0")
    monkeypatch.setenv("HOME_LONGITUDE", "0.0")
    monkeypatch.setenv("DEVIALET_URL", "http://192.168.1.51")
    seen = {}
    monkeypatch.setattr(
        main,
        "build_home_device_tools",
        lambda settings, home, http, found: seen.update(home=home, http=http) or [],
    )
    main.build_registry(main.Settings(_env_file=None), None, "cloud", None, "home-network")
    assert seen == {"home": "home-network", "http": "cloud"}


def test_the_pet_supplies_watch_needs_petkit_and_a_push_channel(monkeypatch, tmp_path):
    base_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    class FakePetKit:
        async def status(self):
            return {}

    devices = {"petkit": FakePetKit()}
    assert main.build_pet_alerts(main.Settings(_env_file=None), devices) is None
    monkeypatch.setenv("NTFY_URL", "https://ntfy.test/topic")
    settings = main.Settings(_env_file=None)
    assert main.build_pet_alerts(settings, {}) is None
    assert main.build_pet_alerts(settings, devices).check_seconds == 180 * 60
    monkeypatch.setenv("PETKIT_ALERTS", "false")
    assert main.build_pet_alerts(main.Settings(_env_file=None), devices) is None


def departure_of(lights_on: bool):
    from intelligent_home_agent.departure import Departure
    from intelligent_home_agent.tools.base import Tool, ToolRegistry

    async def lights(args, ctx):
        room = {"room": "Hall", "lights": [{"name": "Hall", "on": lights_on, "reachable": True}]}
        return {"rooms": [room]}

    async def switch(args, ctx):
        return {"status": "done"}

    schema = {"type": "object"}
    return Departure(
        ToolRegistry(
            [
                Tool("list_hue_lights", "", schema, lights),
                Tool("set_hue_lights", "", schema, switch, needs_clean_turn=True),
            ]
        )
    )


def test_departure_is_off_without_devices(client):
    assert client.post("/v1/departure", json={}, headers=AUTH).status_code == 503
    assert client.post("/v1/departure", json={}).status_code == 401


def test_departure_says_nothing_when_all_is_shut(client):
    client.app.state.departure = departure_of(False)
    body = client.post("/v1/departure", json={}, headers=AUTH).json()
    assert body == {"reply": "", "conversation_id": "default", "pushed": False}


def test_departure_lists_each_fix_waiting_for_a_yes_in_the_conversation(client):
    client.app.state.departure = departure_of(True)
    body = client.post("/v1/departure", json={"conversation_id": "siri"}, headers=AUTH).json()
    assert body["reply"].split("\n") == [
        "🏠 You left, but:",
        "💡 1 light(s) on: Hall.",
        "",
        'Waiting for your go ("yes", or "yes except 2"):',
        "1. Turn off the lights",
    ]
    assert body["follow_up"] is True and body["conversation_id"] == "siri"
    conv = client.app.state.store.peek("siri")
    assert [(p.tool, p.shown) for p in conv.proposals] == [("set_hue_lights", True)]
    assert conv.messages[-1] == {"role": "assistant", "content": body["reply"]}
    # Kept as long as a chat: the yes may come a while after leaving.
    assert conv.ttl_seconds == 12 * 3600


def test_departure_is_pushed_to_ntfy(make_client, respx_mock):
    client = make_client(NTFY_URL="https://ntfy.test/topic", ASSISTANT_NAME="Nova")
    push = respx_mock.post("https://ntfy.test/topic").respond(200)
    client.app.state.departure = departure_of(True)
    body = client.post("/v1/departure", json={"push": True}, headers=AUTH).json()
    assert body["pushed"] is True
    assert push.calls[0].request.url.params["title"] == "Nova · Leaving home"
    assert "1. Turn off the lights" in push.calls[0].request.content.decode()
    assert client.post("/v1/departure", json={"push": True}, headers=AUTH).json()["reply"]


def subscription_env(monkeypatch, **env):
    from intelligent_home_agent import engines

    base_env(monkeypatch)
    # As if the "subscription" extra were installed: nothing here starts Claude Code.
    monkeypatch.setattr(engines, "sdk_installed", lambda: True)
    monkeypatch.setenv("ENGINE", "subscription")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_the_subscription_is_off_by_default_and_only_for_claude(monkeypatch):
    base_env(monkeypatch)
    settings = main.Settings(_env_file=None)
    assert settings.engine == "api" and not settings.subscription_enabled
    assert main.subscription_engine(settings, settings.llm_model) is None
    # A token alone turns nothing on.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    assert not main.Settings(_env_file=None).subscription_enabled

    monkeypatch.setenv("ENGINE", "subscription")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(ValueError, match="claude setup-token"):
        main.Settings(_env_file=None)
    subscription_env(monkeypatch, LLM_PROVIDER="openai", LLM_MODEL="some-model")
    with pytest.raises(ValueError, match="needs LLM_PROVIDER=anthropic"):
        main.Settings(_env_file=None)
    subscription_env(monkeypatch, LLM_PROVIDER="anthropic", SUBSCRIPTION_DISPLAYS="chat, sms")
    with pytest.raises(ValueError, match="sms"):
        main.Settings(_env_file=None)


def test_the_subscription_answers_the_chat_and_the_api_the_rest(monkeypatch, data_dir):
    subscription_env(monkeypatch, SUBSCRIPTION_DISPLAYS="chat, notification")
    fake = object()
    monkeypatch.setattr(main, "build_llm_client", lambda *args: fake)
    settings = main.Settings(_env_file=None)
    assert settings.subscription_enabled
    assert settings.subscription_displays == ["chat", "notification"]

    agent = main.build_agent(settings, None, None)

    engine = agent.engine
    assert engine is main.subscription_engine(settings, settings.llm_model)
    assert engine._model == settings.llm_model and engine._config_dir == data_dir / "claude"
    assert agent._client is fake and agent._api_fallback
    assert agent._engine_displays == {"chat", "notification"}

    # The user's own Claude Code folder, exported in the shell, is never the agent's.
    monkeypatch.setenv("SUBSCRIPTION_MODEL", "claude-test-large")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(data_dir / "elsewhere"))
    other = main.build_agent(main.Settings(_env_file=None), None, None).engine
    assert other is not engine and other._model == "claude-test-large"
    assert other._config_dir == data_dir / "claude"


def test_the_subscription_alone_needs_no_api_key(monkeypatch):
    from intelligent_home_agent.engine import NoApi

    subscription_env(monkeypatch, CODE_EXECUTION_ENABLED="true")
    monkeypatch.delenv("LLM_API_KEY")
    settings = main.Settings(_env_file=None)
    assert settings.llm_api_key is None and settings.subscription_enabled

    agent = main.build_agent(settings, None, None)

    assert isinstance(agent._client, NoApi) and not agent._api_fallback
    assert agent.engine is not None
    # What only the API does is not offered: the code sandbox.
    assert "run_code" not in agent.tools.names()
    # Without the subscription, Claude still needs its key.
    monkeypatch.setenv("ENGINE", "api")
    with pytest.raises(ValueError, match="needs LLM_API_KEY"):
        main.Settings(_env_file=None)


def test_without_the_agent_sdk_the_subscription_is_absent_and_says_so(monkeypatch, caplog):
    from intelligent_home_agent import engines

    subscription_env(monkeypatch)
    monkeypatch.setattr(engines, "sdk_installed", lambda: False)
    fake = object()
    monkeypatch.setattr(main, "build_llm_client", lambda *args: fake)
    settings = main.Settings(_env_file=None)

    agent = main.build_agent(settings, None, None)

    # With an API key the agent runs on it, and the log says what to install.
    assert agent.engine is None and agent._client is fake
    assert 'pip install ".[subscription]"' in caplog.text

    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(RuntimeError, match="isn't installed"):
        main.build_agent(main.Settings(_env_file=None), None, None)


def test_searches_and_scans_go_to_the_subscription_first(monkeypatch):
    subscription_env(monkeypatch, WEB_SEARCH_ENABLED="true")
    settings = main.Settings(_env_file=None)
    seen = {}

    def spy(llm, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(main, "build_web_tools", spy)
    main.build_registry(settings, ha=None, http=None, llm=None)
    # An engine of its own, on the small model the searches use.
    assert seen["engine"] is main.subscription_engine(settings, settings.web_search_model)
    assert seen["engine"]._model == settings.web_search_model != settings.llm_model


def test_an_apple_id_moves_reminders_to_icloud_with_notes_and_find_my(monkeypatch):
    base_env(monkeypatch)
    monkeypatch.setenv("ICLOUD_APPLE_ID", "sam@example.com")
    monkeypatch.setenv("ICLOUD_REMINDER_LISTS", "General, Shopping")
    monkeypatch.setenv("ICLOUD_FIND_MY", "false")
    settings = main.Settings(_env_file=None)
    assert settings.icloud_reminder_lists == ["General", "Shopping"]
    registry = main.build_registry(settings, ha=None, http=None, llm=None)
    names = registry.names()
    assert {"get_reminders", "update_reminder", "complete_reminder", "read_apple_note"} <= names
    assert "find_my_devices" not in names
    # One create_reminder, iCloud's: it waits for the user's go after a read.
    assert registry.get("create_reminder").needs_clean_turn

    monkeypatch.setattr(main, "icloud_available", lambda: False)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert "get_reminders" not in names and "create_reminder" in names


def test_the_remote_browser_needs_its_address_and_its_page_and_a_vpn_key_for_abroad(
    monkeypatch, tmp_path
):
    base_env(monkeypatch)
    monkeypatch.setenv("BROWSER_CDP_HOST", "browser")
    settings = main.Settings(_env_file=None)
    assert "browser_open" not in main.build_registry(settings, None, None, None).names()

    monkeypatch.setenv("BROWSER_PUBLIC_URL", "https://browser.example.com")
    monkeypatch.setenv("WEB_TRUSTED_DOMAINS", "example.org")
    settings = main.Settings(_env_file=None)
    registry = main.build_registry(settings, None, None, None)
    assert {"request_login", "browser_open", "browser_click", "browser_type"} <= registry.names()
    assert "country" not in registry.get("browser_open").input_schema["properties"]
    assert registry.jobs == [] and not main.prompt_config(settings).browser_abroad

    monkeypatch.setenv("VPN_CONTROL_KEY", "k" * 32)
    monkeypatch.setenv("BROWSER_ABROAD_PROXY", "http://10.89.42.30:3129")
    settings = main.Settings(_env_file=None)
    registry = main.build_registry(settings, None, None, None)
    assert "country" in registry.get("browser_open").input_schema["properties"]
    assert len(registry.jobs) == 1 and main.prompt_config(settings).browser_abroad

    monkeypatch.setattr(main, "browser_available", lambda: False)
    assert "browser_open" not in main.build_registry(settings, None, None, None).names()
