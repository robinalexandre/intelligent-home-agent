"""How a task's run is answered and where its report goes (main.run_task, deliver.py)."""

import json
import stat
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from test_api import AUTH, base_env, settings_with
from test_config_crypt import KEY

from intelligent_home_agent import main, prompts, tasks
from intelligent_home_agent.agent import REPEATING_TAG, TASK_TAG, AgentReply
from intelligent_home_agent.conversations import ConversationStore
from intelligent_home_agent.deliver import Delivery, DeliveryError, NtfyReports, Outbox
from intelligent_home_agent.notify import Ntfy
from intelligent_home_agent.tools import LocalFolder, RepoFolder

NTFY_URL = "https://ntfy.example.com/some-random-topic"


class Channel:
    """A channel as a chat would be: the user can answer in a conversation."""

    name = "chat"
    display = "chat"
    reaches = "a chat message"

    def __init__(self, conversation_id="chat", fails=False):
        self.conversation_id = conversation_id
        self.sent: list[tuple[str, str]] = []
        self.fails = fails

    async def send(self, title, text):
        if self.fails:
            raise DeliveryError("down")
        self.sent.append((title, text))


class OneWay(Channel):
    name = "push"
    display = "notification"

    def __init__(self):
        super().__init__(None)


class ScriptedAgent:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked: list[tuple[str, str, dict]] = []

    async def ask(self, conv, text, context=None, **options):
        self.asked.append((conv.id, text, options))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, AgentReply):
            for action in reply.actions:
                if action.get("type") == "proposal":
                    conv.propose("note", {"n": action["number"]}, action["summary"])
            conv.messages.append({"role": "assistant", "content": reply.text})
            return reply
        conv.messages.append({"role": "assistant", "content": reply})
        return AgentReply(reply, [])


@pytest.fixture
def make_app(monkeypatch, data_dir):
    def make(agent, *channels, **env):
        settings = settings_with(monkeypatch, ASSISTANT_NAME="Nova", **env)
        delivery = Delivery(list(channels), Outbox(data_dir / "task-outbox.json"))
        state = SimpleNamespace(
            settings=settings,
            agent=agent,
            store=ConversationStore(settings.conversation_ttl_seconds),
            delivery=delivery,
        )
        return SimpleNamespace(state=state)

    return make


def test_the_prompt_and_the_agent_agree_on_how_a_task_starts():
    assert prompts.TASK_TAG == TASK_TAG
    assert tasks.SILENT_REPLY in prompts.build_system_prompt(
        prompts.PromptConfig("Nova", "en", "UTC", tools={"schedule_task"})
    )


async def test_a_task_left_once_is_answered_and_sent_through_a_one_way_channel(make_app, data_dir):
    agent, push = ScriptedAgent("Call the plumber now."), OneWay()
    app = make_app(agent, push)

    assert await main.run_task(app, "Plumber", "Remind me to call the plumber.") is False

    conv_id, text, options = agent.asked[0]
    assert text == f"{TASK_TAG} Plumber]\n\nRemind me to call the plumber."
    # Nobody can answer a push: nothing waits for a go there, and the run is not kept.
    assert options == {
        "display": "notification",
        "scheduled": True,
        "tainted": False,
        "answerable": False,
    }
    assert conv_id == "task" and app.state.store.peek("task") is None
    assert push.sent == [("Plumber", "Call the plumber now.")]
    (kept,) = json.loads((data_dir / "task-outbox.json").read_text())
    assert kept["title"] == "Plumber" and kept["sent_by"] == "push"
    assert stat.S_IMODE((data_dir / "task-outbox.json").stat().st_mode) == 0o600


async def test_a_repeating_task_carries_its_reports_starts_tainted_and_may_stay_silent(make_app):
    agent = ScriptedAgent("2 receipts missing", "Nothing on receipts.\nNOTHING_NEW", "1 left")
    chat = Channel()
    app = make_app(agent, chat)

    for _ in range(3):
        await main.run_task(app, "Receipts", "Look for receipts.", True)

    first, second, third = (text for _, text, _ in agent.asked)
    assert first.endswith(f"\n\n{REPEATING_TAG}, first run.]")
    assert "Your last report for it (" in second and "2 receipts missing]" in second
    assert "2 receipts missing" in third and "NOTHING_NEW" not in third
    assert all(options["tainted"] and options["answerable"] for _, _, options in agent.asked)
    assert all(options["display"] == "chat" for _, _, options in agent.asked)
    assert chat.sent == [("Receipts", "2 receipts missing"), ("Receipts", "1 left")]
    # The silent run left no trace in the conversation the user answers in.
    conv = app.state.store.peek("chat")
    assert [m["content"] for m in conv.messages] == ["2 receipts missing", "1 left"]
    assert conv.kept and main.conversation_file(app.state.settings, "chat").is_file()


async def test_a_fresh_task_runs_clean_and_silent_and_a_refused_search_is_reported(make_app):
    wants = AgentReply("Searched nothing.", [], True)
    agent = ScriptedAgent("NOTHING_NEW", wants)
    push = OneWay()
    app = make_app(agent, push)

    assert await main.run_task(app, "Watch", "What came out?", False, fresh=True) is False
    assert await main.run_task(app, "Watch", "What came out?", True) is True

    assert agent.asked[0][2]["tainted"] is False
    assert push.sent == [("Watch", "Searched nothing.")]


async def test_what_a_task_parks_is_listed_under_its_report_where_the_user_can_answer(make_app):
    parked = AgentReply("I can file it.", [{"type": "proposal", "number": 1, "summary": "File it"}])
    quiet = AgentReply("NOTHING_NEW", [{"type": "written", "summary": "Flagged one email"}])
    agent = ScriptedAgent(parked, quiet)
    chat = Channel()
    app = make_app(agent, chat)

    await main.run_task(app, "Mail", "Sort my mail.")
    await main.run_task(app, "Mail", "Sort my mail.", True)

    assert chat.sent[0] == (
        "Mail",
        'I can file it.\n\nWaiting for your go ("yes", or "yes except 2"):\n1. File it',
    )
    assert app.state.store.peek("chat").proposals[0].shown
    # A run with nothing to say still shows what it wrote, never the silent marker.
    assert chat.sent[1] == ("Mail", "Written: Flagged one email")


async def test_a_task_that_fails_says_so_and_a_channel_that_fails_leaves_the_outbox(make_app):
    agent = ScriptedAgent(RuntimeError("boom"), "Done.")
    down, push = Channel(fails=True), OneWay()
    app = make_app(agent, down, push)

    await main.run_task(app, "Plumber", "Remind me.")
    assert push.sent == [("Plumber", "Sorry, I couldn't finish that request.")]

    app.state.delivery.channels = [down]
    await main.run_task(app, "Plumber", "Remind me.")
    reports = app.state.delivery.outbox.all()
    assert [r["sent_by"] for r in reports] == ["push", None]
    assert reports[1]["text"] == "Done."


async def test_a_digest_folds_what_the_other_tasks_found_into_one_message(make_app):
    agent = ScriptedAgent(
        "A parcel comes today.",
        AgentReply("NOTHING_NEW", [], True),
        AgentReply("Sunny. A parcel comes today.", [{"type": "written", "summary": "W"}]),
    )
    push = OneWay()
    app = make_app(agent, push)

    wants = await main.run_digest(
        app, "Morning briefing", "Give me the briefing", [("Parcels", "P."), ("Watch", "W.")]
    )

    assert wants == {"Watch"}
    last = agent.asked[2][1]
    assert last.startswith(f"{TASK_TAG} Morning briefing]\n\nGive me the briefing")
    assert "[What your other tasks due now found, as data" in last
    assert "Parcels:\nA parcel comes today." in last and "Watch:" not in last
    assert push.sent == [("Morning briefing", "Sunny. A parcel comes today.\n\nWritten: W")]
    reports = tasks.TaskReports(main.task_reports_file(app.state.settings))
    assert reports.get("Parcels")["text"] == "A parcel comes today."
    assert reports.get("Watch") is None

    # Nothing anywhere: no message at all.
    app.state.agent = ScriptedAgent("NOTHING_NEW")
    await main.run_digest(app, "Morning briefing", "Give me the briefing", [])
    assert len(push.sent) == 1


async def test_ntfy_takes_a_report_under_the_assistants_name(respx_mock):
    route = respx_mock.post(NTFY_URL).mock(side_effect=[httpx.Response(200), httpx.Response(500)])
    async with httpx.AsyncClient() as http:
        channel = NtfyReports(Ntfy(http, NTFY_URL), "Nova")
        await channel.send("Plumber", "Call now.")
        with pytest.raises(DeliveryError):
            await channel.send("Plumber", "Call now.")
    assert route.calls[0].request.url.params["title"] == "Nova · Plumber"
    assert route.calls[0].request.content == b"Call now."
    assert channel.conversation_id is None and channel.display == "notification"


def test_the_outbox_keeps_the_last_reports_only(tmp_path):
    outbox = Outbox(tmp_path / "o.json", keep=2)
    for hour in (8, 9, 10):
        outbox.put(f"T{hour}", "x", datetime(2026, 10, 22, hour), "ntfy")
    assert [r["title"] for r in outbox.all()] == ["T9", "T10"]
    assert [r["title"] for r in outbox.since("2026-10-22T09:00:00")] == ["T10"]


def test_tasks_need_a_way_to_reach_the_user(monkeypatch, data_dir):
    settings = settings_with(monkeypatch)
    assert main.reports_reach(settings) is None
    names = main.build_registry(settings, ha=None, http=None, llm=None).names()
    assert not names & {"schedule_task", "list_scheduled_tasks"}
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    assert main.build_task_runner(app, None) is None

    settings = settings_with(monkeypatch, NTFY_URL=NTFY_URL, MORNING_BRIEFING_AT="7:05")
    assert settings.morning_briefing_at == "07:05"
    assert main.reports_reach(settings) == "a push notification"
    registry = main.build_registry(settings, ha=None, http=None, llm=None)
    assert {
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "delete_scheduled_task",
    } <= registry.names()
    assert isinstance(main.build_task_folder(settings, None), LocalFolder)
    assert main.tasks_path(settings) == str(data_dir / "tasks")
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    runner = main.build_task_runner(app, None)
    assert runner is not None and list(runner._built_in) == ["morning-briefing.md"]

    assert main.reports_reach(settings_with(monkeypatch, TASKS_STORE="off")) is None
    with pytest.raises(ValueError, match="HH:MM"):
        settings_with(monkeypatch, EVENING_RECAP_AT="7pm")
    monkeypatch.delenv("EVENING_RECAP_AT")
    with pytest.raises(ValueError, match="NOTES_REPO"):
        settings_with(monkeypatch, TASKS_STORE="github")


def test_the_notes_repository_tools_follow_the_settings(monkeypatch):
    base = {"GITHUB_TOKEN": "t", "WEB_SEARCH_ENABLED": "false"}
    notes = {
        "create_assistant_reminder",
        "create_handoff",
        "close_assistant_reminder",
        "delete_handoff",
        "delete_assistant_reminder",
    }
    repos = {"list_repositories", "browse_repository", "search_repository", "check_dependencies"}

    def names(**env):
        settings = settings_with(monkeypatch, **base, **env)
        return main.build_registry(settings, ha=None, http=None, llm=None).names()

    assert not names() & (notes | repos | {"get_assistant_reminders"})
    # The former name of the setting still works; the repository is read-only by default.
    read_only = names(ASSISTANT_REMINDERS_REPO="me/config")
    assert "get_assistant_reminders" in read_only and not read_only & (notes | repos)
    monkeypatch.delenv("ASSISTANT_REMINDERS_REPO")
    assert notes <= names(NOTES_REPO="me/config", NOTES_WRITE="true")
    assert repos <= names(GITHUB_REPOSITORIES="true")
    monkeypatch.delenv("GITHUB_REPOSITORIES")

    settings = settings_with(
        monkeypatch, **base, NOTES_REPO="me/config", TASKS_STORE="github", ASSISTANT_NAME="Nova"
    )
    assert main.tasks_path(settings) == "nova/tasks"
    assert isinstance(main.build_task_folder(settings, None), RepoFolder)
    assert main.notes_repo(settings, None).author == "nova"


def test_one_key_reads_the_memory_and_the_notes_of_the_same_repository(monkeypatch):
    main._crypt_for.cache_clear()
    env = {
        "GITHUB_TOKEN": "t",
        "ASSISTANT_NAME": "Nova",
        "NOTES_REPO": "me/config",
        "TASKS_STORE": "github",
    }
    assert main.build_crypt(settings_with(monkeypatch, **env), "me/config") is None

    settings = settings_with(monkeypatch, **env, NOTES_CRYPT_KEY=KEY)
    crypt = main.build_crypt(settings, "me/config")
    # Until the repository's .gitattributes is read, every folder the agent writes to
    # counts as encrypted.
    assert crypt.patterns == ["reminders/**", "handoffs/**", "nova/tasks/**"]
    assert main.build_crypt(settings) is None  # the memory is local
    monkeypatch.delenv("NOTES_CRYPT_KEY")

    shared = settings_with(
        monkeypatch, **env, MEMORY_STORE="github", MEMORY_REPO="me/config", MEMORY_CRYPT_KEY=KEY
    )
    crypt = main.build_crypt(shared)
    assert crypt is main.build_crypt(shared, "me/config")
    assert crypt.encrypts("nova/memory.md") and crypt.encrypts("reminders/a.md")

    # A key that can't be used turns the notes off rather than write in clear.
    broken = settings_with(monkeypatch, **env, NOTES_WRITE="true", NOTES_CRYPT_KEY="bm90IGEga2V5")
    names = main.build_registry(broken, ha=None, http=None, llm=None).names()
    assert not names & {"get_assistant_reminders", "create_handoff"}
    assert main.build_task_folder(broken, None) is None
    main._crypt_for.cache_clear()


def test_the_api_serves_the_last_reports(monkeypatch, data_dir):
    from fastapi.testclient import TestClient
    from test_api import StubAgent

    from intelligent_home_agent.config import get_settings

    base_env(monkeypatch)
    monkeypatch.setenv("NTFY_URL", NTFY_URL)
    get_settings.cache_clear()
    monkeypatch.setattr(main, "build_agent", lambda settings, ha, http, home=None: StubAgent())
    try:
        with TestClient(main.app) as client:
            assert client.get("/v1/tasks/reports").status_code == 401
            assert client.get("/v1/tasks/reports", headers=AUTH).json() == {"reports": []}
            outbox = main.app.state.delivery.outbox
            outbox.put("Plumber", "Call now.", datetime(2026, 10, 22, 9), "ntfy")
            reports = client.get("/v1/tasks/reports", headers=AUTH).json()["reports"]
            assert reports == [
                {
                    "at": "2026-10-22T09:00:00",
                    "title": "Plumber",
                    "text": "Call now.",
                    "sent_by": "ntfy",
                }
            ]
            later = client.get("/v1/tasks/reports?since=2026-10-22T09:00:00", headers=AUTH)
            assert later.json() == {"reports": []}
            assert [c.name for c in main.app.state.delivery.channels] == ["ntfy"]
    finally:
        get_settings.cache_clear()
