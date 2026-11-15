import logging
from contextlib import asynccontextmanager

import pytest
from conftest import FakeLLM, text_response, tool_response

pytest.importorskip("androidtvremote2")  # the "android-tv" extra

from intelligent_home_agent import home_devices  # noqa: E402
from intelligent_home_agent.config import Settings  # noqa: E402
from intelligent_home_agent.engine import NoApi, SubscriptionUnavailable  # noqa: E402
from intelligent_home_agent.home_devices import build_home_device_tools  # noqa: E402
from intelligent_home_agent.llm import LLMError  # noqa: E402
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry  # noqa: E402
from intelligent_home_agent.tools import tv_navigator as nav  # noqa: E402
from intelligent_home_agent.tools.adb import Element  # noqa: E402
from intelligent_home_agent.tools.android_tv import parse_apps  # noqa: E402
from intelligent_home_agent.tools.tv_navigator import (  # noqa: E402
    Navigator,
    build_navigator_tools,
)

FILMS = "com.example.films"
APPS = parse_apps({"films": {"package": FILMS, "link": "films://"}})


class FakeRemote:
    def __init__(self, current_app=FILMS):
        self.current_app = current_app
        self.sent = []
        self.launched = []

    def send_key_command(self, key):
        self.sent.append(key)

    def send_launch_app_command(self, link):
        self.launched.append(link)
        if link == "films://":
            self.current_app = FILMS


class FakeTV:
    def __init__(self, remote, apps=APPS):
        self.remote = remote
        self.apps = apps
        self.woken = []

    @asynccontextmanager
    async def connect(self, wake=False):
        self.woken.append(wake)
        yield self.remote


class FakeScreen:
    def __init__(self, picture=b"jpeg"):
        self.typed = []
        self.picture = picture

    async def elements(self):
        return [Element("Home", True, "[1,1][2,2]"), Element("The race", False, "[3,3][4,4]")]

    async def screenshot(self):
        return self.picture

    async def type_text(self, text):
        self.typed.append(text)


class FakeAdb:
    def __init__(self, screen):
        self.screen = screen

    @asynccontextmanager
    async def session(self):
        yield self.screen


class FakeEngine:
    def __init__(self, script, fail=None):
        self.script = script
        self.fail = fail
        self.available = True
        self.calls = []
        self.system = ""

    async def run_turn(self, system, messages, tools, call, *, max_turns):
        self.system = system
        self.offered = [d["name"] for d in tools.definitions()]
        for name, args in self.script:
            self.calls.append((name, await call(name, args)))
        if self.fail:
            raise SubscriptionUnavailable(self.fail)


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(nav, "KEY_PAUSE", 0)
    monkeypatch.setattr(nav, "SETTLE", 0)


def navigator(remote, screen, *, engine=None, llm=None, max_actions=25, apps=APPS, **more):
    return Navigator(
        FakeTV(remote, apps),
        FakeAdb(screen),
        model="small-model",
        llm=llm,
        engine=engine,
        max_actions=max_actions,
        **more,
    )


async def test_subscription_drives_keys_and_text_then_finishes():
    remote, screen = FakeRemote(), FakeScreen()
    engine = FakeEngine(
        [
            ("press", {"keys": ["down", "right", "ok"]}),
            ("type_text", {"text": "the race"}),
            ("finish", {"success": True, "summary": "The race is playing."}),
        ]
    )
    result = await navigator(remote, screen, engine=engine).navigate("Play the race")
    assert remote.sent == ["KEYCODE_DPAD_DOWN", "KEYCODE_DPAD_RIGHT", "KEYCODE_DPAD_CENTER"]
    assert screen.typed == ["the race"]
    assert result == {
        "success": True,
        "summary": "The race is playing.",
        "actions": 2,
        "ran_on": "subscription",
    }
    assert engine.offered == ["look", "press", "type_text", "open_app", "finish"]
    screen_seen, is_error = engine.calls[0][1]
    assert not is_error
    assert "App in front: films" in screen_seen[0]["text"] and "> Home" in screen_seen[0]["text"]
    assert screen_seen[1]["source"]["media_type"] == "image/jpeg"


async def test_the_summary_is_asked_in_the_users_language():
    english = FakeEngine([("finish", {"success": True, "summary": "x"})])
    french = FakeEngine([("finish", {"success": True, "summary": "x"})])
    await navigator(FakeRemote(), FakeScreen(), engine=english).navigate("x")
    await navigator(FakeRemote(), FakeScreen(), engine=french, language="fr").navigate("x")
    assert "Its summary, in English," in english.system
    assert "Its summary, in French," in french.system


async def test_the_device_is_woken_and_the_app_opened_first():
    remote = FakeRemote(current_app="com.google.android.tvlauncher")
    tv = FakeTV(remote)
    engine = FakeEngine([("look", {}), ("finish", {"success": False, "summary": "x"})])
    helper = Navigator(tv, FakeAdb(FakeScreen()), model="m", engine=engine)
    await helper.navigate("x", "films")
    assert tv.woken == [True] and remote.launched == ["films://"]
    assert "App in front: films" in engine.calls[0][1][0][0]["text"]


async def test_an_app_that_does_not_open_is_said_before_any_model_runs(monkeypatch):
    async def stays_closed(remote, app):
        return False

    monkeypatch.setattr(nav, "open_app", stays_closed)
    engine = FakeEngine([("finish", {"success": True, "summary": "x"})])
    with pytest.raises(ToolError, match="did not open"):
        await navigator(FakeRemote(), FakeScreen(), engine=engine).navigate("x", "films")
    assert engine.calls == []


async def test_blank_screen_says_a_protected_video_may_play():
    remote, screen = FakeRemote(), FakeScreen(picture=None)
    engine = FakeEngine([("look", {}), ("finish", {"success": True, "summary": "ok"})])
    await navigator(remote, screen, engine=engine).navigate("x")
    seen = engine.calls[0][1][0]
    assert len(seen) == 1
    assert "protected video" in seen[0]["text"]


async def test_without_a_subscription_the_providers_api_drives_it():
    remote, screen = FakeRemote(), FakeScreen()
    llm = FakeLLM(
        [
            tool_response(("t1", "press", {"keys": ["back"]})),
            tool_response(("t2", "finish", {"success": True, "summary": "Went back."})),
        ]
    )
    result = await navigator(remote, screen, llm=llm).navigate("Go back")
    assert result == {"success": True, "summary": "Went back.", "actions": 1, "ran_on": "api"}
    assert remote.sent == ["KEYCODE_BACK"]
    first, second = llm.requests
    assert first["model"] == "small-model" and first["messages"][0]["content"] == "Goal: Go back"
    assert [t["name"] for t in first["tools"]] == [
        "look",
        "press",
        "type_text",
        "open_app",
        "finish",
    ]
    # The screen after the press went back to the model as text and a picture, in dict blocks.
    result_block = second["messages"][2]["content"][0]
    assert result_block["type"] == "tool_result" and result_block["tool_use_id"] == "t1"
    assert [b["type"] for b in result_block["content"]] == ["text", "image"]


async def test_a_reply_without_a_call_ends_the_run_with_its_words():
    llm = FakeLLM([text_response("Nothing to do here.")])
    result = await navigator(FakeRemote(), FakeScreen(), llm=llm).navigate("x")
    assert result["summary"] == "Nothing to do here." and not result["success"]


async def test_api_rounds_are_capped_even_when_every_call_is_refused():
    refused = tool_response(("t", "press", {"keys": ["power"]}))
    llm = FakeLLM([refused] * 50)
    result = await navigator(FakeRemote(), FakeScreen(), llm=llm, max_actions=3).navigate("x")
    assert len(llm.requests) == 5
    assert result["ran_on"] == "api"


async def test_falls_back_to_the_api_when_the_subscription_cannot():
    remote, screen = FakeRemote(), FakeScreen()
    engine = FakeEngine([], fail="usage limit")
    llm = FakeLLM(
        [
            tool_response(("t1", "press", {"keys": ["back"]})),
            tool_response(("t2", "finish", {"success": True, "summary": "Went back."})),
        ]
    )
    result = await navigator(remote, screen, engine=engine, llm=llm).navigate("x")
    assert result["ran_on"] == "api"
    assert result["summary"] == "Went back."
    assert remote.sent == ["KEYCODE_BACK"]


async def test_a_run_the_subscription_finished_is_not_done_again_on_the_api():
    engine = FakeEngine([("finish", {"success": True, "summary": "Done."})], fail="cut off")
    llm = FakeLLM([])
    result = await navigator(FakeRemote(), FakeScreen(), engine=engine, llm=llm).navigate("x")
    assert result["ran_on"] == "subscription" and llm.requests == []


async def test_no_model_left_is_an_error_for_the_agent_not_a_crash():
    engine = FakeEngine([], fail="usage limit")
    with pytest.raises(ToolError, match="No model can drive"):
        await navigator(FakeRemote(), FakeScreen(), engine=engine).navigate("x")
    # The subscription alone (no API key): NoApi says what is missing.
    with pytest.raises(ToolError, match="no API key is set"):
        await navigator(FakeRemote(), FakeScreen(), engine=engine, llm=NoApi()).navigate("x")

    class Refusing:
        async def create(self, **kwargs):
            raise LLMError("this model takes no pictures")

    with pytest.raises(ToolError, match="takes no pictures"):
        await navigator(FakeRemote(), FakeScreen(), llm=Refusing()).navigate("x")


async def test_actions_are_capped():
    remote, screen = FakeRemote(), FakeScreen()
    engine = FakeEngine([("press", {"keys": ["up"]})] * 3)
    await navigator(remote, screen, engine=engine, max_actions=2).navigate("x")
    assert remote.sent == ["KEYCODE_DPAD_UP"] * 2
    assert engine.calls[2][1] == ("Out of actions (2): call finish now.", True)


async def test_unknown_keys_and_apps_are_refused():
    remote, screen = FakeRemote(), FakeScreen()
    engine = FakeEngine([("press", {"keys": ["power"]}), ("open_app", {"app": "elsewhere"})])
    await navigator(remote, screen, engine=engine).navigate("x")
    assert engine.calls[0][1][1] is True
    assert engine.calls[1][1][1] is True
    assert remote.sent == [] and remote.launched == []


def test_older_screens_lose_their_picture():
    image = {"type": "image", "source": {}}
    text = {"type": "text", "text": "screen"}
    history = [{"role": "user", "content": "Goal"}]
    for i in range(4):
        history.append({"role": "assistant", "content": [{"type": "tool_use", "id": str(i)}]})
        history.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": str(i), "content": [text, image]}
                ],
            }
        )
    trimmed = nav._older_screens_dropped(history)
    pictures = [
        any(b["type"] == "image" for b in m["content"][0]["content"]) for m in trimmed[2::2]
    ]
    assert pictures == [False, False, True, True]


async def test_tool_wants_a_goal_and_a_known_app(ctx):
    tool = build_navigator_tools(navigator(FakeRemote(), FakeScreen()), "projector")[0]
    assert tool.name == "navigate_tv" and tool.needs_clean_turn and tool.taints
    assert "the projector" in tool.description
    assert tool.input_schema["properties"]["app"]["enum"] == list(APPS)
    with pytest.raises(ToolError, match="Say what"):
        await tool.handler({"goal": " "}, ctx)
    with pytest.raises(ToolError, match="Unknown app"):
        await tool.handler({"goal": "x", "app": "elsewhere"}, ctx)
    assert build_navigator_tools(None) == []


async def test_a_device_without_apps_offers_none_to_open():
    engine = FakeEngine([("finish", {"success": True, "summary": "x"})])
    bare = navigator(FakeRemote(), FakeScreen(), engine=engine, apps={})
    await bare.navigate("x")
    assert "open_app" not in engine.offered
    assert "app" not in build_navigator_tools(bare)[0].input_schema["properties"]


async def test_it_taints_the_request_and_waits_after_a_read(ctx):
    engine = FakeEngine([("finish", {"success": True, "summary": "Playing."})])
    tools = ToolRegistry(
        build_navigator_tools(navigator(FakeRemote(), FakeScreen(), engine=engine))
    )
    assert tools.taints("navigate_tv")
    result = await tools.run("navigate_tv", {"goal": "Play it"}, ctx)
    assert result["summary"] == "Playing."
    after_mail = ToolContext(conversation=ctx.conversation, tainted=True)
    parked = await tools.run("navigate_tv", {"goal": "Play it"}, after_mail)
    assert parked["status"] == "waiting_for_the_user" and len(engine.calls) == 1
    assert "Play it" in after_mail.actions[0]["summary"]


# Wiring (home_devices.py): on only when asked for, with adb and a model to drive it.

ANDROID_TV = {
    "ANDROID_TV_HOST": "192.168.1.60",
    "ANDROID_TV_CLIENT_CERT": "Q0VSVA==",
    "ANDROID_TV_CLIENT_KEY": "S0VZ",
}


def device_tools(monkeypatch, tmp_path, env, llm=None, adb="/usr/bin/adb"):
    for name in ("ENGINE", "CLAUDE_CODE_OAUTH_TOKEN", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    base = {"AGENT_API_TOKEN": "t", "LLM_API_KEY": "k", "DATA_DIR": str(tmp_path)}
    for key, value in {**base, **ANDROID_TV, **env}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(home_devices.shutil, "which", lambda name: adb)
    settings = Settings(_env_file=None)
    return settings, {t.name for t in build_home_device_tools(settings, llm=llm)}


def test_the_navigator_is_off_until_asked_for(monkeypatch, tmp_path):
    _, found = device_tools(monkeypatch, tmp_path, {}, llm=FakeLLM([]))
    assert found == {"get_tv_state", "control_tv"}
    settings, found = device_tools(
        monkeypatch, tmp_path, {"ANDROID_TV_NAVIGATOR": "true"}, llm=FakeLLM([])
    )
    assert found == {"get_tv_state", "control_tv", "navigate_tv"}
    # The key ADB pairs with lives with what the agent keeps.
    assert (tmp_path / "android" / ".android").is_dir()
    assert settings.android_tv_navigator_model == "claude-haiku-5-5"


def test_another_provider_drives_it_with_its_own_model(monkeypatch, tmp_path):
    env = {"ANDROID_TV_NAVIGATOR": "true", "LLM_PROVIDER": "mistral", "LLM_MODEL": "sees-pictures"}
    settings, found = device_tools(monkeypatch, tmp_path, env, llm=FakeLLM([]))
    assert "navigate_tv" in found and settings.android_tv_navigator_model == "sees-pictures"
    env["ANDROID_TV_NAVIGATOR_MODEL"] = "small-and-fast"
    settings, _ = device_tools(monkeypatch, tmp_path, env, llm=FakeLLM([]))
    assert settings.android_tv_navigator_model == "small-and-fast"


def test_without_adb_or_a_model_it_stays_off_and_the_log_says_why(monkeypatch, tmp_path, caplog):
    on = {"ANDROID_TV_NAVIGATOR": "true"}
    with caplog.at_level(logging.WARNING):
        _, found = device_tools(monkeypatch, tmp_path, on, llm=FakeLLM([]), adb=None)
    assert found == {"get_tv_state", "control_tv"}
    assert "adb program is not installed" in caplog.text and "navigate_tv stays off" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _, found = device_tools(monkeypatch, tmp_path, on, llm=None)
        _, keyless = device_tools(monkeypatch, tmp_path, on, llm=NoApi())
    assert "navigate_tv" not in found | keyless
    assert caplog.text.count("neither a Claude subscription nor an API") == 2
