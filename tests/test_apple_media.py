import asyncio
import json

import pytest

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.tools.apple_media import (
    AppleMedia,
    build_apple_media_tools,
    open_tunnel,
    parse_devices,
    route_through_proxy,
)
from intelligent_home_agent.tools.base import ToolContext, ToolError, ToolRegistry

PROFILE = json.dumps(
    [
        {
            "name": "Living room TV",
            "host": "192.0.2.25",
            "id": "TV",
            "services": {
                "AirPlay": {"port": 7000, "properties": {"model": "AppleTV6,2"}},
                "Companion": {"port": 49153, "properties": {}},
            },
        },
        {
            "name": "Office",
            "host": "192.0.2.20",
            "id": "HP",
            "services": {"AirPlay": {"port": 7000, "properties": {"model": "AudioAccessory5,1"}}},
        },
    ]
)


def test_the_pairing_goes_to_the_apple_tv_only():
    tv, homepod = parse_devices(PROFILE, {"Companion": "c", "AirPlay": "a"})
    assert tv.is_apple_tv and tv.credentials == {"Companion": "c", "AirPlay": "a"}
    assert not homepod.is_apple_tv and homepod.credentials == {}


def test_a_broken_profile_says_so():
    with pytest.raises(ValueError, match="APPLE_DEVICES"):
        parse_devices("[{")


async def echo_server():
    async def echo(reader, writer):
        while data := await reader.read(100):
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_server(echo, "127.0.0.1", 0)


async def connect_proxy(target_port, answer=b"HTTP/1.1 200 Connection established\r\n\r\n"):
    """An HTTP proxy that takes any CONNECT to the local echo server."""
    asked = []

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        asked.append(head.split(b"\r\n", 1)[0].decode())
        writer.write(answer)
        await writer.drain()
        if not answer.startswith(b"HTTP/1.1 200"):
            writer.close()
            return
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", target_port)

        async def pipe(src, dst):
            while data := await src.read(100):
                dst.write(data)
                await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, asked


class Recorder(asyncio.Protocol):
    def __init__(self):
        self.received = asyncio.get_running_loop().create_future()

    def connection_made(self, transport):
        self.peer = transport.get_extra_info("socket").getpeername()
        self.peername = transport.get_extra_info("peername")

    def data_received(self, data):
        if not self.received.done():
            self.received.set_result(data)


async def test_routed_connections_go_through_the_proxy_naming_the_device():
    echo = await echo_server()
    proxy, asked = await connect_proxy(echo.sockets[0].getsockname()[1])
    loop = asyncio.get_running_loop()
    original = loop.create_connection
    try:
        route_through_proxy(
            loop, f"http://127.0.0.1:{proxy.sockets[0].getsockname()[1]}", {"192.0.2.20"}
        )
        transport, protocol = await loop.create_connection(Recorder, "192.0.2.20", 7000)
        transport.write(b"hello")
        assert await asyncio.wait_for(protocol.received, 5) == b"hello"
        assert asked == ["CONNECT 192.0.2.20:7000 HTTP/1.1"]
        assert protocol.peer == ("192.0.2.20", 7000) == protocol.peername
        assert transport.get_extra_info("socket").getpeername() == ("192.0.2.20", 7000)
        transport.close()
        # Any other host goes out as before.
        port = echo.sockets[0].getsockname()[1]
        other, _ = await loop.create_connection(Recorder, "127.0.0.1", port)
        other.close()
        assert len(asked) == 1
    finally:
        loop.create_connection = original
        proxy.close()
        echo.close()


async def test_a_refused_tunnel_is_an_error():
    proxy, _ = await connect_proxy(1, answer=b"HTTP/1.1 403 Forbidden\r\n\r\n")
    loop = asyncio.get_running_loop()
    try:
        with pytest.raises(ConnectionError, match="refused 192.0.2.20:7000"):
            await open_tunnel(
                loop, f"127.0.0.1:{proxy.sockets[0].getsockname()[1]}", "192.0.2.20", 7000
            )
    finally:
        proxy.close()


class FakeMedia(AppleMedia):
    def __init__(self):
        super().__init__(parse_devices(PROFILE))
        self.acted = []

    async def state(self, device):
        if device.name == "Office":
            raise ToolError("Office is unreachable")
        return {"device": device.name, "kind": "apple_tv", "on": True, "playing": True}

    async def act(self, device, action):
        self.acted.append((device.name, action))


async def test_the_tools_read_every_device_and_act_on_one():
    media = FakeMedia()
    tools = ToolRegistry(build_apple_media_tools(media))
    ctx = ToolContext(Conversation(id="t"))
    state = await tools.run("get_apple_media_state", {}, ctx)
    assert state["devices"][0]["on"] is True
    assert state["devices"][1] == {
        "device": "Office",
        "reachable": False,
        "note": "Office is unreachable",
    }
    done = await tools.run("control_apple_media", {"device": "office", "action": "pause"}, ctx)
    assert done == {"status": "done", "device": "Office", "action": "pause"}
    assert media.acted == [("Office", "pause")]
    with pytest.raises(ToolError, match="No Apple device"):
        await tools.run("control_apple_media", {"device": "Kitchen", "action": "pause"}, ctx)
