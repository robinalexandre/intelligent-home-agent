import json

import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools.devialet import Devialet, build_devialet_tools

BASE = "http://192.168.1.30/ipcontrol/v1"
SOURCES = {
    "sources": [
        {"sourceId": "a1", "type": "airplay2"},
        {"sourceId": "o1", "type": "optical"},
        {"sourceId": "b1", "type": "bluetooth"},
    ]
}


def tools(http, max_volume=70):
    return {
        t.name: t for t in build_devialet_tools(Devialet(http, "http://192.168.1.30/"), max_volume)
    }


def ok():
    return httpx.Response(200, json={})


@respx.mock
async def test_state_gathers_volume_playback_and_track(ctx):
    respx.get(f"{BASE}/devices/current").mock(
        return_value=httpx.Response(200, json={"deviceName": "Phantom Ultimate 98 dB"})
    )
    respx.get(f"{BASE}/groups/current/sources/current").mock(
        return_value=httpx.Response(
            200,
            json={
                "playingState": "playing",
                "muteState": "unmuted",
                "source": {"type": "airplay2"},
                "metadata": {"title": "Teardrop", "artist": "Massive Attack", "coverArtUrl": "x"},
            },
        )
    )
    respx.get(f"{BASE}/systems/current/sources/current/soundControl/volume").mock(
        return_value=httpx.Response(200, json={"volume": 38})
    )
    respx.get(f"{BASE}/groups/current/sources").mock(return_value=httpx.Response(200, json=SOURCES))
    async with httpx.AsyncClient() as http:
        result = await tools(http)["get_speaker_state"].handler({}, ctx)
        track = await tools(http)["get_speaker_track"].handler({}, ctx)

    assert result["volume"] == 38
    assert result["playing"] == "playing"
    assert result["source"] == "airplay2"
    assert result["sources"] == ["airplay2", "bluetooth", "optical"]
    # Whoever streams writes the track's words: only the tainting tool shows them.
    assert "Teardrop" not in str(result)
    assert track == {"now_playing": {"title": "Teardrop", "artist": "Massive Attack"}}
    assert tools(http)["get_speaker_track"].taints
    assert not tools(http)["get_speaker_state"].taints


@respx.mock
async def test_volume_is_capped(ctx):
    volume = respx.post(f"{BASE}/systems/current/sources/current/soundControl/volume").mock(
        return_value=ok()
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http, max_volume=60)["control_speaker"].handler({"volume": 95}, ctx)

    assert json.loads(volume.calls[0].request.content) == {"volume": 60}
    assert result["volume"] == 60
    assert "Capped" in result["note"]


@respx.mock
async def test_source_volume_step_and_playback_in_one_call(ctx):
    respx.get(f"{BASE}/groups/current/sources").mock(return_value=httpx.Response(200, json=SOURCES))
    source = respx.post(f"{BASE}/groups/current/sources/o1/playback/play").mock(return_value=ok())
    up = respx.post(f"{BASE}/systems/current/sources/current/soundControl/volumeUp").mock(
        return_value=ok()
    )
    pause = respx.post(f"{BASE}/groups/current/sources/current/playback/unmute").mock(
        return_value=ok()
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["control_speaker"].handler(
            {"source": "optical", "volume_step": "up", "playback": "unmute"}, ctx
        )

    assert source.called and up.called and pause.called
    assert result == {
        "status": "done",
        "source": "optical",
        "volume_step": "up",
        "playback": "unmute",
    }


@respx.mock
async def test_missing_input_lists_the_real_ones(ctx):
    respx.get(f"{BASE}/groups/current/sources").mock(return_value=httpx.Response(200, json=SOURCES))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="Inputs: bluetooth, optical"):
            await tools(http)["control_speaker"].handler({"source": "hdmi"}, ctx)


@respx.mock
async def test_standby(ctx):
    off = respx.post(f"{BASE}/systems/current/powerOff").mock(return_value=ok())
    async with httpx.AsyncClient() as http:
        await tools(http)["control_speaker"].handler({"standby": True}, ctx)

    assert off.called


@respx.mock
async def test_errors_are_explained(ctx):
    respx.get(f"{BASE}/devices/current").mock(side_effect=httpx.ConnectTimeout("slow"))
    respx.post(f"{BASE}/groups/current/sources/current/playback/next").mock(
        return_value=httpx.Response(400, json={"error": {"details": "operation not available"}})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="unreachable"):
            await tools(http)["get_speaker_state"].handler({}, ctx)
        with pytest.raises(ToolError, match="operation not available"):
            await tools(http)["control_speaker"].handler({"playback": "next"}, ctx)


async def test_nothing_to_change_is_refused(ctx):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="Say what to change"):
            await tools(http)["control_speaker"].handler({}, ctx)


def test_control_needs_a_clean_request():
    tool_map = {t.name: t for t in build_devialet_tools(Devialet(None, "http://x"))}
    assert tool_map["control_speaker"].needs_clean_turn
    assert not tool_map["get_speaker_state"].needs_clean_turn
