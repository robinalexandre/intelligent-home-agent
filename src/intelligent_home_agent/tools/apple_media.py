"""The Apple TV and the HomePods, through pyatv: what plays, pause, play, turn off.

An agent outside the home reaches its network only by TCP through an HTTP proxy
(HOME_PROXY_URL), so mDNS discovery can't run from it: each device comes from
APPLE_DEVICES, a profile of the mDNS properties pyatv needs, taken once on the home network
(scripts/apple_devices.py profile). The same profile is used when the agent runs at home.
The Apple TV is paired once (scripts/apple_devices.py pair, a PIN on the TV); HomePods need
no pairing.

pyatv opens its own connections, the AirPlay remote control channel among them, on ports
the device gives. It runs on its own event loop in a thread, whose connections to these
hosts go through the proxy (``route_through_proxy``); the tunnel reports the device, not
the proxy, as its peer, as pyatv opens the next channels to the address its first
connection reports. The agent's own loop is left as it is.

Needs the ``apple`` extra (pyatv with srptools 1.x, pinned: srptools 2 breaks the AirPlay
remote control channel of pyatv 0.18); without it the feature stays off (home_devices.py).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from typing import Any
from urllib.parse import urlsplit

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

TIMEOUT_SECONDS = 15.0
ACTIONS = ("pause", "play", "turn_off")


@dataclass
class AppleDevice:
    name: str
    host: str
    id: str
    # Protocol name (AirPlay, Companion) -> {"port", "properties"}.
    services: dict[str, dict[str, Any]]
    credentials: dict[str, str] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return str(self.services.get("AirPlay", {}).get("properties", {}).get("model", ""))

    @property
    def is_apple_tv(self) -> bool:
        return self.model.startswith("AppleTV")


def parse_devices(raw: str, credentials: dict[str, str] | None = None) -> list[AppleDevice]:
    """APPLE_DEVICES as scripts/apple_devices.py profile prints it; the Apple TV's pairing
    (protocol -> credentials) goes to every Apple TV listed."""
    try:
        listed = json.loads(raw) if raw.strip() else []
    except ValueError as exc:
        raise ValueError(f"APPLE_DEVICES is not valid JSON: {exc}") from exc
    devices = []
    for item in listed if isinstance(listed, list) else [None]:
        try:
            device = AppleDevice(item["name"], item["host"], item["id"], item["services"])
        except (KeyError, TypeError):
            raise ValueError(
                "APPLE_DEVICES is not a profile: paste what `python scripts/apple_devices.py "
                "profile <addresses>` prints, a list of devices with name, host, id, services."
            ) from None
        if device.is_apple_tv and credentials:
            device.credentials = {k: v for k, v in credentials.items() if v}
        devices.append(device)
    return devices


class _PeerSocket:
    def __init__(self, sock: Any, peer: tuple[str, int]) -> None:
        self._sock = sock
        self._peer = peer

    def getpeername(self) -> tuple[str, int]:
        return self._peer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


class _PeerTransport:
    """The tunnel's transport, naming the device as its peer instead of the proxy."""

    def __init__(self, transport: Any, peer: tuple[str, int]) -> None:
        self._transport = transport
        self._peer = peer

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "peername":
            return self._peer
        if name == "socket":
            return _PeerSocket(self._transport.get_extra_info("socket"), self._peer)
        return self._transport.get_extra_info(name, default)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)


async def open_tunnel(
    loop: asyncio.AbstractEventLoop, proxy: str, host: str, port: int
) -> socket.socket:
    """A socket to host:port through the HTTP proxy (CONNECT)."""
    url = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    infos = await loop.getaddrinfo(url.hostname, url.port or 80, type=socket.SOCK_STREAM)
    family, kind, proto, _, address = infos[0]
    sock = socket.socket(family, kind, proto)
    sock.setblocking(False)
    try:
        await loop.sock_connect(sock, address)
        request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        await loop.sock_sendall(sock, request.encode())
        head = b""
        # Byte by byte: what follows the proxy's answer belongs to the device.
        while not head.endswith(b"\r\n\r\n"):
            chunk = await loop.sock_recv(sock, 1)
            if not chunk:
                raise ConnectionError("The home network proxy closed the connection.")
            head += chunk
            if len(head) > 4096:
                raise ConnectionError("The home network proxy sent no readable answer.")
        status = head.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"200":
            raise ConnectionError(f"The home network proxy refused {host}:{port}.")
    except BaseException:
        sock.close()
        raise
    return sock


def route_through_proxy(loop: asyncio.AbstractEventLoop, proxy: str, hosts: set[str]) -> None:
    """Send this loop's TCP connections to these hosts through the proxy; others as before."""
    original = loop.create_connection

    async def create_connection(factory: Callable[[], Any], host=None, port=None, **kwargs):
        if host not in hosts or kwargs.get("sock") is not None:
            return await original(factory, host, port, **kwargs)
        peer = (host, port)

        def named() -> Any:
            protocol = factory()
            made = protocol.connection_made
            protocol.connection_made = lambda transport: made(_PeerTransport(transport, peer))
            return protocol

        sock = await open_tunnel(loop, proxy, host, port)
        kwargs.pop("family", None)
        kwargs.pop("local_addr", None)
        transport, protocol = await original(named, sock=sock, **kwargs)
        return _PeerTransport(transport, peer), protocol

    loop.create_connection = create_connection  # type: ignore[method-assign]


class AppleMedia:
    def __init__(self, devices: list[AppleDevice], proxy: str | None = None) -> None:
        self.devices = devices
        self._proxy = proxy
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _own_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            loop = asyncio.new_event_loop()
            if self._proxy:
                route_through_proxy(loop, self._proxy, {d.host for d in self.devices})
            threading.Thread(target=loop.run_forever, name="apple-media", daemon=True).start()
            self._loop = loop
        return self._loop

    def find(self, wanted: str | None) -> list[AppleDevice]:
        if not wanted:
            return list(self.devices)
        key = wanted.casefold().strip()
        exact = [d for d in self.devices if d.name.casefold() == key]
        found = exact or [d for d in self.devices if key in d.name.casefold()]
        if not found:
            names = ", ".join(d.name for d in self.devices)
            raise ToolError(f"No Apple device named {wanted!r}. Devices: {names}.")
        return found

    async def _connect(self, device: AppleDevice) -> Any:
        import pyatv
        from pyatv.conf import AppleTV, ManualService
        from pyatv.const import Protocol

        loop = asyncio.get_running_loop()
        conf = AppleTV(IPv4Address(device.host), device.name)
        for name, service in device.services.items():
            conf.add_service(
                ManualService(
                    device.id,
                    Protocol[name],
                    int(service["port"]),
                    dict(service.get("properties") or {}),
                    credentials=device.credentials.get(name),
                )
            )
        return await pyatv.connect(conf, loop)

    async def _with(self, device: AppleDevice, act: Callable[[Any], Any]) -> Any:
        """Connect, act and close on pyatv's own loop; awaited from the agent's."""
        async with self._lock:
            done = asyncio.run_coroutine_threadsafe(self._session(device, act), self._own_loop())
            return await asyncio.wrap_future(done)

    async def _session(self, device: AppleDevice, act: Callable[[Any], Any]) -> Any:
        try:
            atv = await asyncio.wait_for(self._connect(device), TIMEOUT_SECONDS)
        except (OSError, TimeoutError) as exc:
            raise ToolError(
                f"{device.name} is unreachable (asleep off the network, or the home "
                f"network can't be reached): {type(exc).__name__}."
            ) from exc
        except Exception as exc:
            raise ToolError(f"{device.name} refused the connection: {exc}") from exc
        try:
            return await asyncio.wait_for(act(atv), TIMEOUT_SECONDS)
        finally:
            for task in atv.close():
                await task

    async def state(self, device: AppleDevice) -> dict[str, Any]:
        async def read(atv: Any) -> dict[str, Any]:
            from pyatv.const import DeviceState, PowerState

            playing = await atv.metadata.playing()
            power = atv.power.power_state
            app = None
            if device.is_apple_tv:
                try:
                    app = atv.metadata.app.name if atv.metadata.app else None
                except Exception:
                    app = None
            row = {
                "device": device.name,
                "kind": "apple_tv" if device.is_apple_tv else "homepod",
                "on": power != PowerState.Off,
                "playing": playing.device_state == DeviceState.Playing,
                "state": playing.device_state.name.lower(),
                "media": playing.media_type.name.lower(),
                "app": app,
            }
            return {k: v for k, v in row.items() if v is not None}

        return await self._with(device, read)

    async def act(self, device: AppleDevice, action: str) -> None:
        async def run(atv: Any) -> None:
            if action == "pause":
                await atv.remote_control.pause()
            elif action == "play":
                await atv.remote_control.play()
            elif action == "turn_off":
                await atv.power.turn_off()

        await self._with(device, run)


def build_apple_media_tools(media: AppleMedia) -> list[Tool]:
    async def get_apple_media_state(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        rows = []
        for device in media.find(args.get("device")):
            try:
                rows.append(await media.state(device))
            except ToolError as exc:
                rows.append({"device": device.name, "reachable": False, "note": str(exc)})
        return {"devices": rows}

    async def control_apple_media(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        action = args.get("action")
        if action not in ACTIONS:
            raise ToolError(f"Say what to do: {', '.join(ACTIONS)}.")
        devices = media.find(args.get("device"))
        if len(devices) > 1:
            raise ToolError("Say which device: " + ", ".join(d.name for d in devices) + ".")
        await media.act(devices[0], action)
        return {"status": "done", "device": devices[0].name, "action": action}

    names = ", ".join(d.name for d in media.devices)
    return [
        Tool(
            name="get_apple_media_state",
            description=(
                f"The Apple TV and the HomePods ({names}): whether each is on, plays "
                "(and what kind of media), and the Apple TV's app in front. A HomePod paired "
                "with the Apple TV as its speaker shows the TV's state. Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "One device; empty for all."}
                },
            },
            handler=get_apple_media_state,
        ),
        Tool(
            name="control_apple_media",
            description=(
                f"Pause or play on the Apple TV or a HomePod ({names}), or turn the Apple "
                "TV off (sleep). A HomePod can't be turned off: pause it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                },
                "required": ["device", "action"],
            },
            handler=control_apple_media,
            needs_clean_turn=True,
        ),
    ]
