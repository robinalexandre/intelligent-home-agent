import json

import httpx
import pytest
from fastapi.testclient import TestClient

from intelligent_home_agent import main
from intelligent_home_agent.agent import AgentReply
from intelligent_home_agent.config import get_settings

NTFY_URL = "https://ntfy.example.com/some-random-topic"


class StubAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.notifications: list[bool] = []

    async def ask(self, conv, text: str, context: str | None = None, *, notification=False):
        self.calls.append((conv.id, text, context))
        self.notifications.append(notification)
        actions = [{"type": "reminder", "title": text}] if text.startswith("remind") else []
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


def test_notification_display_is_passed_to_the_agent(client):
    client.post("/v1/ask", json={"text": "Briefing", "display": "notification"}, headers=AUTH)
    client.post("/v1/ask", json={"text": "Weather"}, headers=AUTH)
    assert client.stub.notifications == [True, False]
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
    assert client.stub.notifications == [True]


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
        "get_github_pull_requests",
        "get_assistant_reminders",
        "web_search",
    }


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
    monkeypatch.setenv("DEVIALET_URL", "http://192.168.1.30")
    monkeypatch.setenv("HA_PROXY_URL", "http://proxy.test:1055")
    settings = main.Settings(_env_file=None)
    assert main.has_home_devices(settings)
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert {"set_hue_lights", "activate_hue_scene", "control_speaker"} <= names
