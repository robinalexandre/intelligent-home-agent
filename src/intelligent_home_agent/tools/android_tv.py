"""A Google TV / Android TV device (a TV, a projector, a streaming box), through the
Android TV Remote protocol v2.

It is the protocol of Google's own TV remote app: TLS on port 6466 with a client certificate
the device accepted once, after a code shown on its screen (scripts/android_tv_pair.py).
The library opens plain sockets, so when the agent runs outside the home each connection
goes through a local forwarder that tunnels it with HTTP CONNECT through the home network
proxy (HOME_PROXY_URL).

In standby the remote port closes on many devices, but Google Cast keeps listening when the
device's energy settings allow it: asking Cast to launch its default receiver wakes the
device, the way casting from a phone does. Apps open through their deep links; Google TV no
longer opens an app from its package id alone.

Needs the ``android-tv`` extra (androidtvremote2, pinned: an unofficial library); without
it the feature stays off (home_devices.py).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import ssl
import struct
import tempfile
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

API_PORT = 6466
PAIR_PORT = 6467
CAST_PORT = 8009
CLIENT_NAME = "Home agent"
# How long to wait for the device to wake, or for an app to come to the front.
WAIT_SECONDS = 6.0
# A live TV app needs a moment after opening before it takes a channel number.
CHANNEL_DELAY = 4.0


class App(NamedTuple):
    package: str
    # None: no deep link, so the app opens from its Play Store page, where "Open" has focus.
    # Only for apps known to be installed: on a missing app that button installs it.
    link: str | None


# The apps every installation knows; ANDROID_TV_APPS adds or replaces some.
DEFAULT_APPS = {
    "netflix": App("com.netflix.ninja", "netflix://"),
    "youtube": App("com.google.android.youtube.tv", "https://www.youtube.com"),
    "prime video": App("com.amazon.amazonvideo.livingroom", "https://app.primevideo.com"),
    "disney+": App("com.disney.disneyplus", "https://www.disneyplus.com"),
}
STORE = "com.android.vending"


def parse_apps(extra: dict[str, Any] | None) -> dict[str, App]:
    """The default apps plus the user's: {"name": {"package": ..., "link": ... or null}};
    a name set to null removes a default one."""
    apps = dict(DEFAULT_APPS)
    for name, value in (extra or {}).items():
        key = str(name).strip().lower()
        if value is None:
            apps.pop(key, None)
            continue
        if not isinstance(value, dict) or not value.get("package"):
            raise ValueError(f"ANDROID_TV_APPS: {name!r} needs a package (and a link, or null).")
        apps[key] = App(str(value["package"]), value.get("link") or None)
    return apps


def parse_channels(listed: dict[str, Any] | None) -> dict[str, int]:
    """Live channels by name -> the number typed in the live TV app."""
    channels = {}
    for name, number in (listed or {}).items():
        if not isinstance(number, int) or not 0 < number < 1000:
            raise ValueError(f"ANDROID_TV_CHANNELS: {name!r} needs a number from 1 to 999.")
        channels[_plain(str(name))] = number
    return channels


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


HOME_SCREENS = {"com.google.android.apps.tv.launcherx", "com.google.android.tvlauncher"}
SCREENSAVER = "com.google.android.apps.tv.dreamx"
CAST_SCREEN = "com.google.android.apps.mediashell"
PLAYBACK = {
    "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "play": "KEYCODE_MEDIA_PLAY",
    "pause": "KEYCODE_MEDIA_PAUSE",
    "stop": "KEYCODE_MEDIA_STOP",
    "next": "KEYCODE_MEDIA_NEXT",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
}
UNREACHABLE = (
    "No answer from the TV device: it is off at the wall or in deep standby, or the home "
    "network can't be reached."
)

CAST_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
CAST_RECEIVER = "urn:x-cast:com.google.cast.receiver"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"


class TVUnreachable(ToolError):
    pass


def encode_pem(pem: bytes) -> str:
    """How a certificate or key is kept in a setting: one line of base64."""
    return base64.b64encode(pem).decode()


class Tunnel:
    """A port on 127.0.0.1 whose connections reach host:port through an HTTP CONNECT proxy."""

    def __init__(self, proxy: str, host: str, port: int, timeout: float) -> None:
        url = urlparse(proxy)
        self._proxy = (url.hostname or "", url.port or 80)
        self._target = f"{host}:{port}"
        self._timeout = timeout
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def __aexit__(self, *_: object) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    async def _handle(self, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(*self._proxy), self._timeout
            )
            up_w.write(f"CONNECT {self._target} HTTP/1.1\r\nHost: {self._target}\r\n\r\n".encode())
            await up_w.drain()
            status = await asyncio.wait_for(up_r.readline(), self._timeout)
            while (await asyncio.wait_for(up_r.readline(), self._timeout)).strip():
                pass
        except (OSError, TimeoutError):
            client_w.close()
            return
        if b" 200 " not in status:
            up_w.close()
            client_w.close()
            return
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte, n = n & 0x7F, n >> 7
        if not n:
            return out + bytes([byte])
        out += bytes([byte | 0x80])


def cast_message(namespace: str, payload: dict[str, Any]) -> bytes:
    """A Cast channel CastMessage (protobuf, string payload), length-prefixed."""

    def text(number: int, value: str) -> bytes:
        data = value.encode()
        return bytes([number << 3 | 2]) + _varint(len(data)) + data

    body = (
        b"\x08\x00"  # protocol_version CASTV2_1_0
        + text(2, "sender-0")
        + text(3, "receiver-0")
        + text(4, namespace)
        + b"\x28\x00"  # payload_type STRING
        + text(6, json.dumps(payload))
    )
    return struct.pack(">I", len(body)) + body


class AndroidTV:
    def __init__(
        self,
        host: str,
        cert: str,
        key: str,
        proxy: str | None = None,
        timeout: float = 8.0,
        apps: dict[str, App] | None = None,
        channel_app: str | None = None,
        channels: dict[str, int] | None = None,
        name: str = "TV",
    ) -> None:
        """cert and key: the client certificate and its key, PEM in base64 (encode_pem).
        channel_app: the app (one of apps) that takes a live channel's number typed on
        the remote; channels: names for those numbers. name: what the user calls it."""
        self.apps = dict(DEFAULT_APPS) if apps is None else apps
        self.channel_app = (channel_app or "").strip().lower() or None
        if self.channel_app and self.channel_app not in self.apps:
            raise ValueError(
                f"ANDROID_TV_CHANNEL_APP {channel_app!r} is not one of the apps: "
                f"{', '.join(self.apps)}."
            )
        self.channels = channels or {}
        self.name = name
        self._host = host
        self._proxy = proxy
        self._timeout = timeout
        self._lock = asyncio.Lock()
        folder = Path(tempfile.mkdtemp(prefix="android-tv-"))
        self._certfile = folder / "cert.pem"
        self._keyfile = folder / "key.pem"
        for path, value in ((self._certfile, cert), (self._keyfile, key)):
            try:
                pem = base64.b64decode(value, validate=True)
            except ValueError:
                raise ValueError(
                    "ANDROID_TV_CLIENT_CERT and ANDROID_TV_CLIENT_KEY are the two lines "
                    "`python scripts/android_tv_pair.py <address>` prints (base64)."
                ) from None
            path.write_bytes(pem)
            os.chmod(path, 0o600)

    async def _address(self, stack: AsyncExitStack, port: int) -> tuple[str, int]:
        if not self._proxy:
            return self._host, port
        tunnel = Tunnel(self._proxy, self._host, port, self._timeout)
        return "127.0.0.1", await stack.enter_async_context(tunnel)

    async def _open(self, stack: AsyncExitStack) -> AndroidTVRemote:
        host, port = await self._address(stack, API_PORT)
        remote = AndroidTVRemote(
            CLIENT_NAME, str(self._certfile), str(self._keyfile), host, api_port=port
        )
        try:
            await asyncio.wait_for(remote.async_connect(), self._timeout)
        except InvalidAuth as exc:
            raise ToolError(
                "The TV device no longer accepts the agent: pair it again "
                "(scripts/android_tv_pair.py)."
            ) from exc
        except ssl.SSLError as exc:
            # Read when connecting: base64 of something that is no certificate and key.
            raise ToolError(
                "ANDROID_TV_CLIENT_CERT and ANDROID_TV_CLIENT_KEY are not a certificate and "
                "its key: pair the TV device again (scripts/android_tv_pair.py)."
            ) from exc
        except (CannotConnect, ConnectionClosed, TimeoutError) as exc:
            remote.disconnect()
            raise TVUnreachable(UNREACHABLE) from exc
        stack.callback(remote.disconnect)
        await _settle(remote, 1.5)
        return remote

    async def wake(self) -> bool:
        """Wake the device from standby through Google Cast; True once it says so."""
        context = ssl.create_default_context()
        # Cast devices present self-signed certificates.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        async with AsyncExitStack() as stack:
            host, port = await self._address(stack, CAST_PORT)
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=context), self._timeout
                )
            except (OSError, TimeoutError):
                return False
            stack.callback(writer.close)
            launch = {"type": "LAUNCH", "appId": DEFAULT_MEDIA_RECEIVER, "requestId": 1}
            writer.write(cast_message(CAST_CONNECTION, {"type": "CONNECT"}))
            writer.write(cast_message(CAST_RECEIVER, launch))
            try:
                await writer.drain()
                for _ in range(10):
                    size = await asyncio.wait_for(reader.readexactly(4), self._timeout)
                    data = await reader.readexactly(struct.unpack(">I", size)[0])
                    if b'"isStandBy":false' in data:
                        return True
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                pass
        return False

    @asynccontextmanager
    async def connect(self, wake: bool = False) -> AsyncIterator[AndroidTVRemote]:
        """The remote, connected. wake: from standby, wake the device first."""
        async with self._lock, AsyncExitStack() as stack:
            try:
                remote = await self._open(stack)
            except TVUnreachable:
                if not wake or not await self.wake():
                    raise
                remote = await self._open_after_wake(stack)
            yield remote

    async def _open_after_wake(self, stack: AsyncExitStack) -> AndroidTVRemote:
        for _ in range(int(WAIT_SECONDS)):
            await asyncio.sleep(2)
            with contextlib.suppress(TVUnreachable):
                return await _leave_cast_screen(await self._open(stack))
        raise TVUnreachable("The TV device woke up but its remote did not answer.")


async def _leave_cast_screen(remote: AndroidTVRemote) -> AndroidTVRemote:
    """Waking through Cast leaves its empty receiver in front: go to the home screen."""
    if remote.current_app == CAST_SCREEN:
        remote.send_key_command("KEYCODE_HOME")
        await _wait(lambda: remote.current_app != CAST_SCREEN, WAIT_SECONDS)
    return remote


async def _settle(remote: AndroidTVRemote, seconds: float) -> None:
    """The app in front and the volume arrive just after the connection: wait for them."""
    for _ in range(int(seconds / 0.1)):
        if remote.current_app and remote.volume_info:
            return
        await asyncio.sleep(0.1)


def app_name(package: str | None, apps: dict[str, App] | None = None) -> str | None:
    if not package:
        return None
    if package in HOME_SCREENS:
        return "home screen"
    if package == SCREENSAVER:
        return "screensaver"
    if package == CAST_SCREEN:
        return "cast screen"
    known = DEFAULT_APPS if apps is None else apps
    return next((name for name, app in known.items() if app.package == package), package)


def _state(remote: AndroidTVRemote, apps: dict[str, App] | None = None) -> dict[str, Any]:
    volume = remote.volume_info or {}
    device = remote.device_info or {}
    level, top = volume.get("level"), volume.get("max")
    return {
        "device": " ".join(filter(None, (device.get("manufacturer"), device.get("model")))) or None,
        "on": bool(remote.is_on),
        "app": app_name(remote.current_app, apps) if remote.is_on else None,
        "volume": round(100 * level / top) if level is not None and top else None,
        "muted": volume.get("muted"),
    }


def _channel_number(channel: str, channels: dict[str, int]) -> int:
    wanted = _plain(channel)
    if wanted.isdigit() and 0 < int(wanted) < 1000:
        return int(wanted)
    if wanted in channels:
        return channels[wanted]
    known = f"Channels: {', '.join(channels)}, or a number." if channels else "Give its number."
    raise ToolError(f"Unknown channel {channel!r}. {known}")


def build_android_tv_tools(tv: AndroidTV, max_volume: int = 70) -> list[Tool]:
    apps, channels = tv.apps, tv.channels

    async def get_tv_state(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        try:
            async with tv.connect() as remote:
                return _state(remote, apps)
        except TVUnreachable as exc:
            return {"on": False, "reachable": False, "note": str(exc)}

    async def control_tv(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        power, app, channel = args.get("power"), args.get("app"), args.get("channel")
        if app is not None and app not in apps:
            raise ToolError(f"Unknown app {app!r}. Apps: {', '.join(apps)}.")
        if channel is not None:
            if not tv.channel_app:
                raise ToolError("No live TV app is set up (ANDROID_TV_CHANNEL_APP): open an app.")
            _channel_number(str(channel), channels)
        wanted = ("app", "channel", "volume", "volume_step", "mute", "playback")
        if power is None and not any(args.get(k) is not None for k in wanted):
            raise ToolError(
                "Say what to change: power, app, channel, volume, volume_step, mute, playback."
            )
        try:
            async with tv.connect(wake=power != "off") as remote:
                return await _control(remote, args, max_volume, tv)
        except TVUnreachable as exc:
            if power == "off":
                return {"status": "done", "power": "off", "note": "Already off or unreachable."}
            raise ToolError(f"{exc} Waking it through Google Cast did not work either.") from exc

    what = f"The {tv.name} (a Google TV / Android TV device)"
    properties: dict[str, Any] = {
        "power": {"type": "string", "enum": ["on", "off"]},
        "app": {"type": "string", "enum": list(apps)},
        "volume": {"type": "integer", "minimum": 0, "maximum": 100},
        "volume_step": {"type": "string", "enum": ["up", "down"]},
        "mute": {"type": "boolean"},
        "playback": {"type": "string", "enum": list(PLAYBACK)},
    }
    live = ""
    if tv.channel_app:
        named = f"by name ({', '.join(channels)}) or number" if channels else "by number"
        properties["channel"] = {"type": "string", "description": f"A live channel, {named}."}
        live = f"put a live TV channel on through {tv.channel_app} ({named}), "
    return [
        Tool(
            name="get_tv_state",
            description=(
                f"{what}: whether it is on, the app in front, its volume (0-100) and whether "
                "it is muted."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_tv_state,
        ),
        Tool(
            name="control_tv",
            description=(
                f"Control {what[0].lower()}{what[1:]}: turn it on (it wakes from standby) or "
                f"off, open an app ({', '.join(apps)}), {live}set the volume (0-100, capped at "
                f"{max_volume}) or step it up/down, mute or unmute, play, pause, stop, next, "
                "previous, go home or back."
            ),
            input_schema={"type": "object", "properties": properties},
            handler=control_tv,
            needs_clean_turn=True,
        ),
    ]


async def open_app(remote: AndroidTVRemote, app: App) -> bool:
    if remote.current_app == app.package:
        return True
    if app.link:
        remote.send_launch_app_command(app.link)
    else:
        remote.send_launch_app_command(
            f"https://play.google.com/store/apps/details?id={app.package}"
        )
        await _wait(lambda: remote.current_app == STORE, WAIT_SECONDS)
        if remote.current_app != STORE:
            return False
        await asyncio.sleep(3)
        remote.send_key_command("KEYCODE_DPAD_CENTER")
    await _wait(lambda: remote.current_app == app.package, WAIT_SECONDS)
    return remote.current_app == app.package


async def _control(
    remote: AndroidTVRemote, args: dict[str, Any], max_volume: int, tv: AndroidTV
) -> dict[str, Any]:
    done: dict[str, Any] = {}
    power, app, channel = args.get("power"), args.get("app"), args.get("channel")
    if power == "off":
        if remote.is_on:
            remote.send_key_command("KEYCODE_POWER")
        return {"status": "done", "power": "off"}
    if not remote.is_on:
        remote.send_key_command("KEYCODE_POWER")
        await _wait(lambda: remote.is_on, WAIT_SECONDS)
        if not remote.is_on:
            raise ToolError("The TV device did not turn on.")
        done["power"] = "on"
    elif power == "on":
        done["power"] = "already on"
    if channel is not None:
        app = tv.channel_app
    if app is not None:
        if not await open_app(remote, tv.apps[app]):
            raise ToolError(f"{app} did not open: it may not be installed.")
        done["app"] = app
    if channel is not None:
        await asyncio.sleep(CHANNEL_DELAY)
        for digit in str(_channel_number(str(channel), tv.channels)):
            remote.send_key_command(f"KEYCODE_{digit}")
            await asyncio.sleep(0.3)
        done["channel"] = channel
    volume = remote.volume_info
    if (target := args.get("volume")) is not None:
        if not volume or not volume.get("max"):
            raise ToolError("The TV device does not report its volume, so it can't be set.")
        percent = max(0, min(int(target), max_volume))
        steps = round(percent * volume["max"] / 100) - volume["level"]
        key = "KEYCODE_VOLUME_UP" if steps > 0 else "KEYCODE_VOLUME_DOWN"
        for _ in range(abs(steps)):
            remote.send_key_command(key)
        done["volume"] = percent
        if percent < int(target):
            done["volume_note"] = f"Capped at {max_volume}, the most the user allows."
    elif (step := args.get("volume_step")) in ("up", "down"):
        remote.send_key_command(f"KEYCODE_VOLUME_{step.upper()}")
        done["volume_step"] = step
    if (mute := args.get("mute")) is not None:
        if volume is None or bool(volume.get("muted")) != mute:
            remote.send_key_command("KEYCODE_VOLUME_MUTE")
        done["muted"] = mute
    if (action := args.get("playback")) is not None:
        remote.send_key_command(PLAYBACK[action])
        done["playback"] = action
    await asyncio.sleep(0.3)
    return {"status": "done", **done}


async def _wait(condition: Callable[[], Any], seconds: float) -> None:
    for _ in range(int(seconds / 0.2)):
        if condition():
            return
        await asyncio.sleep(0.2)
