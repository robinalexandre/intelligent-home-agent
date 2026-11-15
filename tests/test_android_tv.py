import asyncio
import base64
import os
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("androidtvremote2")  # the "android-tv" extra

from intelligent_home_agent.tools import ToolError  # noqa: E402
from intelligent_home_agent.tools import android_tv as mod  # noqa: E402
from intelligent_home_agent.tools.android_tv import (  # noqa: E402
    AndroidTV,
    App,
    Tunnel,
    TVUnreachable,
    build_android_tv_tools,
    encode_pem,
    parse_apps,
    parse_channels,
)

# A live TV app with no deep link: it opens from its store page.
LIVE = "com.example.livetv"
APPS = parse_apps({"live tv": {"package": LIVE, "link": None}})
CHANNELS = parse_channels({"News One": 2, "Ciné Two": 15})


class FakeRemote:
    def __init__(self, is_on=True, app="com.netflix.ninja", level=10, top=20, muted=False):
        self.is_on = is_on
        self.current_app = app
        self.volume_info = {"level": level, "max": top, "muted": muted}
        self.device_info = {"manufacturer": "ACME", "model": "TV-1", "sw_version": "1"}
        self.sent: list[str] = []
        self.launched: list[str] = []
        self.installed = {app.package for app in APPS.values()}

    def send_key_command(self, key):
        self.sent.append(key)
        if key == "KEYCODE_POWER":
            self.is_on = not self.is_on
        if key == "KEYCODE_DPAD_CENTER" and self.current_app == mod.STORE:
            self.current_app = self.store_page

    def send_launch_app_command(self, link):
        self.launched.append(link)
        if link.startswith("https://play.google.com/store/apps/details?id="):
            self.current_app = mod.STORE
            self.store_page = link.rsplit("=", 1)[1]
            return
        package = next(app.package for app in APPS.values() if app.link == link)
        if package in self.installed:
            self.current_app = package


class FakeTV:
    apps = APPS
    channels = CHANNELS
    channel_app = "live tv"
    name = "TV"

    def __init__(self, remote=None):
        self.remote = remote
        self.woken: list[bool] = []

    @asynccontextmanager
    async def connect(self, wake=False):
        self.woken.append(wake)
        if self.remote is None:
            raise TVUnreachable(mod.UNREACHABLE)
        yield self.remote


def tools(remote=None, max_volume=70, tv=None):
    tv = tv or FakeTV(remote)
    return {t.name: t for t in build_android_tv_tools(tv, max_volume)}


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    monkeypatch.setattr(mod, "WAIT_SECONDS", 0.4)
    monkeypatch.setattr(mod, "CHANNEL_DELAY", 0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))


async def test_state_names_the_app_and_scales_the_volume(ctx):
    state = await tools(FakeRemote())["get_tv_state"].handler({}, ctx)
    assert state == {
        "device": "ACME TV-1",
        "on": True,
        "app": "netflix",
        "volume": 50,
        "muted": False,
    }


async def test_state_calls_the_launcher_the_home_screen(ctx):
    remote = FakeRemote(app="com.google.android.apps.tv.launcherx")
    state = await tools(remote)["get_tv_state"].handler({}, ctx)
    assert state["app"] == "home screen"
    remote = FakeRemote(app="com.google.android.apps.tv.dreamx")
    state = await tools(remote)["get_tv_state"].handler({}, ctx)
    assert state["app"] == "screensaver"


async def test_state_when_unreachable_says_off(ctx):
    state = await tools()["get_tv_state"].handler({}, ctx)
    assert state["on"] is False
    assert state["reachable"] is False


async def test_power_off_presses_power_only_when_on(ctx):
    remote = FakeRemote()
    await tools(remote)["control_tv"].handler({"power": "off"}, ctx)
    assert remote.sent == ["KEYCODE_POWER"]
    remote = FakeRemote(is_on=False)
    await tools(remote)["control_tv"].handler({"power": "off"}, ctx)
    assert remote.sent == []


async def test_power_off_when_unreachable_is_done(ctx):
    result = await tools()["control_tv"].handler({"power": "off"}, ctx)
    assert result["status"] == "done"


async def test_power_on_asks_to_wake_and_says_when_even_that_fails(ctx):
    tv = FakeTV()
    with pytest.raises(ToolError, match="Google Cast did not work"):
        await tools(tv=tv)["control_tv"].handler({"power": "on"}, ctx)
    assert tv.woken == [True]


async def test_power_off_never_wakes(ctx):
    tv = FakeTV()
    await tools(tv=tv)["control_tv"].handler({"power": "off"}, ctx)
    assert tv.woken == [False]


async def test_an_app_without_a_link_opens_through_its_store_page(ctx):
    remote = FakeRemote(app="com.google.android.apps.tv.launcherx")
    result = await tools(remote)["control_tv"].handler({"app": "live tv"}, ctx)
    assert remote.launched == [f"https://play.google.com/store/apps/details?id={LIVE}"]
    assert remote.sent == ["KEYCODE_DPAD_CENTER"]
    assert remote.current_app == LIVE
    assert result["app"] == "live tv"


async def test_channel_opens_the_live_app_and_types_the_number(ctx):
    remote = FakeRemote(app="com.google.android.apps.tv.launcherx")
    result = await tools(remote)["control_tv"].handler({"channel": "news one"}, ctx)
    assert remote.sent == ["KEYCODE_DPAD_CENTER", "KEYCODE_2"]
    assert result["channel"] == "news one"
    remote = FakeRemote(app=LIVE)
    await tools(remote)["control_tv"].handler({"channel": "15"}, ctx)
    assert remote.launched == []
    assert remote.sent == ["KEYCODE_1", "KEYCODE_5"]


async def test_unknown_channel_is_refused(ctx):
    with pytest.raises(ToolError, match="Unknown channel"):
        await tools(FakeRemote())["control_tv"].handler({"channel": "nowhere"}, ctx)


def test_cast_message_is_a_length_prefixed_cast_frame():
    frame = mod.cast_message(mod.CAST_RECEIVER, {"type": "LAUNCH"})
    size = int.from_bytes(frame[:4], "big")
    assert size == len(frame) - 4
    assert frame[4:6] == b"\x08\x00"
    assert b"receiver-0" in frame
    assert frame.endswith(b'{"type": "LAUNCH"}')


async def test_power_on_when_already_on_presses_nothing(ctx):
    remote = FakeRemote()
    result = await tools(remote)["control_tv"].handler({"power": "on"}, ctx)
    assert remote.sent == []
    assert result["power"] == "already on"


async def test_app_wakes_the_device_then_opens(ctx):
    remote = FakeRemote(is_on=False, app="")
    result = await tools(remote)["control_tv"].handler({"app": "youtube"}, ctx)
    assert remote.sent == ["KEYCODE_POWER"]
    assert remote.launched == ["https://www.youtube.com"]
    assert result["app"] == "youtube"
    assert "note" not in result


async def test_app_not_in_front_says_it_may_be_missing(ctx):
    remote = FakeRemote()
    remote.installed = set()
    with pytest.raises(ToolError, match="may not be installed"):
        await tools(remote)["control_tv"].handler({"app": "disney+"}, ctx)


async def test_unknown_app_is_refused(ctx):
    with pytest.raises(ToolError, match="Unknown app"):
        await tools(FakeRemote())["control_tv"].handler({"app": "nowhere"}, ctx)


async def test_volume_steps_to_the_target_and_caps_it(ctx):
    remote = FakeRemote(level=10, top=20)
    result = await tools(remote)["control_tv"].handler({"volume": 30}, ctx)
    assert remote.sent == ["KEYCODE_VOLUME_DOWN"] * 4
    assert result["volume"] == 30
    remote = FakeRemote(level=10, top=20)
    result = await tools(remote, max_volume=60)["control_tv"].handler({"volume": 90}, ctx)
    assert remote.sent == ["KEYCODE_VOLUME_UP"] * 2
    assert result["volume"] == 60
    assert "Capped" in result["volume_note"]


async def test_mute_toggles_only_when_needed(ctx):
    remote = FakeRemote(muted=True)
    await tools(remote)["control_tv"].handler({"mute": True}, ctx)
    assert remote.sent == []
    await tools(remote)["control_tv"].handler({"mute": False}, ctx)
    assert remote.sent == ["KEYCODE_VOLUME_MUTE"]


async def test_playback_and_empty_request(ctx):
    remote = FakeRemote()
    await tools(remote)["control_tv"].handler({"playback": "pause"}, ctx)
    assert remote.sent == ["KEYCODE_MEDIA_PAUSE"]
    with pytest.raises(ToolError, match="Say what to change"):
        await tools(remote)["control_tv"].handler({}, ctx)


def test_control_needs_a_clean_turn():
    assert tools(FakeRemote())["control_tv"].needs_clean_turn


def test_credentials_land_in_private_files():
    tv = AndroidTV("192.168.1.60", encode_pem(b"CERT"), encode_pem(b"KEY"))
    assert tv._certfile.read_bytes() == b"CERT"
    assert tv._keyfile.read_bytes() == b"KEY"
    assert os.stat(tv._keyfile).st_mode & 0o777 == 0o600
    assert base64.b64decode(encode_pem(b"x")) == b"x"


async def _echo(reader, writer):
    while data := await reader.read(1024):
        writer.write(data.upper())
        await writer.drain()
    writer.close()


def _proxy(echo_port, status=b"200 Connection established"):
    targets = []

    async def handle(reader, writer):
        request = await reader.readline()
        while (await reader.readline()).strip():
            pass
        targets.append(request.split()[1].decode())
        writer.write(b"HTTP/1.1 " + status + b"\r\n\r\n")
        await writer.drain()
        if not status.startswith(b"200"):
            writer.close()
            return
        up_r, up_w = await asyncio.open_connection("127.0.0.1", echo_port)
        await asyncio.gather(mod._pipe(reader, up_w), mod._pipe(up_r, writer))

    return handle, targets


async def test_tunnel_reaches_the_target_through_connect():
    echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
    handle, targets = _proxy(echo.sockets[0].getsockname()[1])
    proxy = await asyncio.start_server(handle, "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
    async with Tunnel(proxy_url, "192.168.1.60", 6466, 2) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"hello")
        await writer.drain()
        assert await reader.readexactly(5) == b"HELLO"
        writer.close()
    assert targets == ["192.168.1.60:6466"]
    proxy.close()
    echo.close()


async def test_tunnel_closes_when_the_proxy_refuses():
    echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
    handle, _ = _proxy(echo.sockets[0].getsockname()[1], b"500 Internal Server Error")
    proxy = await asyncio.start_server(handle, "127.0.0.1", 0)
    proxy_url = f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
    async with Tunnel(proxy_url, "192.168.1.60", 6466, 2) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        writer.close()
    proxy.close()
    echo.close()


def test_apps_are_the_defaults_plus_the_users():
    apps = parse_apps({"Live TV": {"package": LIVE}, "disney+": None})
    assert apps["live tv"] == App(LIVE, None)
    assert "netflix" in apps and "disney+" not in apps
    with pytest.raises(ValueError, match="package"):
        parse_apps({"broken": {"link": "x://"}})


def test_channels_ignore_case_and_accents_and_need_a_number():
    assert CHANNELS == {"news one": 2, "cine two": 15}
    assert mod._channel_number("CINE two", CHANNELS) == 15
    with pytest.raises(ValueError, match="number"):
        parse_channels({"News": "two"})


def test_the_channel_app_must_be_a_known_app():
    with pytest.raises(ValueError, match="ANDROID_TV_CHANNEL_APP"):
        AndroidTV("192.168.1.60", encode_pem(b"C"), encode_pem(b"K"), channel_app="live tv")


async def test_without_a_live_app_there_is_no_channel(ctx):
    class NoLive(FakeTV):
        channel_app = None
        channels = {}

    control = tools(tv=NoLive(FakeRemote()))["control_tv"]
    assert "channel" not in control.input_schema["properties"]
    assert "live TV" not in control.description
    with pytest.raises(ToolError, match="No live TV app"):
        await control.handler({"channel": "2"}, ctx)


def test_descriptions_name_the_device_as_the_user_does():
    class Beamer(FakeTV):
        name = "projector"

    described = tools(tv=Beamer(FakeRemote()))
    assert described["get_tv_state"].description.startswith("The projector (a Google TV")
    assert "live tv (by name (news one, cine two) or number)" in described["control_tv"].description
