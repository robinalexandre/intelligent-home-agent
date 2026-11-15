import asyncio
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError, Withings, WithingsAuth, build_withings_tools
from intelligent_home_agent.tools.withings import MEASURE_URL, TOKEN_URL

TZ = "Europe/Paris"


def token_response(refresh="r2", status=0):
    body = {"access_token": "a1", "expires_in": 10800, "refresh_token": refresh}
    return httpx.Response(200, json={"status": status, "body": body if status == 0 else {}})


def group(days_ago, weight, fat=None, attrib=0, now=None):
    at = (now or datetime.now().astimezone()) - timedelta(days=days_ago)
    measures = [{"value": round(weight * 1000), "unit": -3, "type": 1}]
    if fat is not None:
        measures.append({"value": round(fat * 10), "unit": -1, "type": 6})
    return {"date": int(at.timestamp()), "attrib": attrib, "category": 1, "measures": measures}


def measures_response(groups, more=False, offset=0):
    body = {"measuregrps": groups, "more": int(more), "offset": offset}
    return httpx.Response(200, json={"status": 0, "body": body})


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def tool(http, tmp_path, refresh="r1"):
    auth = WithingsAuth(http, "c1", "s1", refresh, tmp_path / "withings.json")
    return build_withings_tools(Withings(http, auth), TZ)[0]


@respx.mock
async def test_weigh_ins_come_with_the_latest_and_their_trends(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    groups = [group(31, 71.0), group(20, 70.0, 25.0), group(6, 69.4, 24.5), group(0, 69.0, 24.1)]
    measures = respx.post(MEASURE_URL).mock(return_value=measures_response(groups))
    async with httpx.AsyncClient() as http:
        result = await tool(http, tmp_path).handler({"days": 7}, ctx)

    assert result["latest"]["weight_kg"] == 69.0
    assert result["latest"]["fat_percent"] == 24.1
    assert result["last_7_days"]["change"] == {"weight_kg": -0.4, "fat_percent": -0.4}
    assert result["last_7_days"]["weigh_ins"] == 2
    assert result["last_30_days"]["change"] == {"weight_kg": -1.0, "fat_percent": -0.9}
    assert [w["weight_kg"] for w in result["weigh_ins"]] == [69.4, 69.0]
    # The user's own data: no new site opens after it.
    assert ctx.account_read
    sent = form(measures.calls[0].request)
    assert sent["action"] == "getmeas"
    assert sent["category"] == "1"
    assert "1" in sent["meastypes"].split(",")
    assert measures.calls[0].request.headers["authorization"] == "Bearer a1"


@respx.mock
async def test_the_measures_of_one_weigh_in_are_merged_and_ambiguous_ones_left_out(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    now = datetime.now().astimezone()
    weight = group(0, 69.0, now=now)
    pulse = {**weight, "measures": [{"value": 62, "unit": 0, "type": 11}]}
    someone_else = group(1, 55.0, attrib=1, now=now)
    respx.post(MEASURE_URL).mock(return_value=measures_response([someone_else, weight, pulse]))
    async with httpx.AsyncClient() as http:
        result = await tool(http, tmp_path).handler({}, ctx)

    assert len(result["weigh_ins"]) == 1
    assert result["latest"]["weight_kg"] == 69.0
    assert result["latest"]["heart_rate_bpm"] == 62
    assert result["last_7_days"] is None


@respx.mock
async def test_every_page_is_read(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    route = respx.post(MEASURE_URL).mock(
        side_effect=[
            measures_response([group(3, 70.0)], more=True, offset=1),
            measures_response([group(1, 69.5)]),
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await tool(http, tmp_path).handler({}, ctx)

    assert [w["weight_kg"] for w in result["weigh_ins"]] == [70.0, 69.5]
    assert "offset" not in form(route.calls[0].request)
    assert form(route.calls[1].request)["offset"] == "1"


@respx.mock
async def test_no_weigh_in_says_so(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.post(MEASURE_URL).mock(return_value=measures_response([]))
    async with httpx.AsyncClient() as http:
        result = await tool(http, tmp_path).handler({}, ctx)

    assert result["weigh_ins"] == 0


@respx.mock
async def test_rotated_refresh_token_is_saved_and_reused(tmp_path):
    store = tmp_path / "withings.json"
    route = respx.post(TOKEN_URL).mock(return_value=token_response("r2"))
    async with httpx.AsyncClient() as http:
        assert await WithingsAuth(http, "c1", "s1", "r1", store).access_token() == "a1"
        assert json.loads(store.read_text()) == {"client_id": "c1", "refresh_token": "r2"}
        await WithingsAuth(http, "c1", "s1", "r1", store).access_token()

    first = form(route.calls[0].request)
    assert first["action"] == "requesttoken"
    assert first["grant_type"] == "refresh_token"
    assert first["client_secret"] == "s1"
    assert first["refresh_token"] == "r1"
    assert form(route.calls[1].request)["refresh_token"] == "r2"
    assert oct(store.stat().st_mode)[-3:] == "600"


@respx.mock
async def test_concurrent_calls_refresh_once(tmp_path):
    route = respx.post(TOKEN_URL).mock(return_value=token_response())
    async with httpx.AsyncClient() as http:
        auth = WithingsAuth(http, "c1", "s1", "r1", tmp_path / "w.json")
        await asyncio.gather(auth.access_token(), auth.access_token())

    assert route.call_count == 1


@respx.mock
async def test_an_expired_token_is_renewed_once(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(side_effect=[token_response("r2"), token_response("r3")])
    respx.post(MEASURE_URL).mock(
        side_effect=[
            httpx.Response(200, json={"status": 401, "body": {}}),
            measures_response([group(0, 69.0)]),
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await tool(http, tmp_path).handler({}, ctx)

    assert result["latest"]["weight_kg"] == 69.0


@respx.mock
async def test_a_refused_refresh_says_to_log_in_again(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response(status=503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="withings_login.py again"):
            await tool(http, tmp_path).handler({}, ctx)


@respx.mock
async def test_the_rate_limit_asks_to_wait(tmp_path, ctx):
    respx.post(TOKEN_URL).mock(return_value=token_response())
    respx.post(MEASURE_URL).mock(return_value=httpx.Response(200, json={"status": 601}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="try again later"):
            await tool(http, tmp_path).handler({}, ctx)


async def test_without_login_it_says_how_to_connect(tmp_path):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="withings_login"):
            await WithingsAuth(http, "c1", "s1", None, tmp_path / "w.json").access_token()
