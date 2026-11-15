import json

import httpx
import pytest
import respx

from intelligent_home_agent.tools.base import ToolError, ToolRegistry
from intelligent_home_agent.tools.nuki import API_URL, Nuki, build_nuki_tools
from intelligent_home_agent.tools.unlock_codes import UnlockCodes

TOKEN = "nuki-token"


def lock(smartlock_id, name, state=1, door=2, kind=4, server=0, **extra):
    return {
        "smartlockId": smartlock_id,
        "name": name,
        "type": kind,
        "serverState": server,
        "updateDate": "2026-11-13T08:30:00.000Z",
        "state": {
            "mode": 2,
            "state": state,
            "doorState": door,
            "batteryCritical": False,
            "batteryCharge": 81,
            "nightMode": False,
            **extra,
        },
    }


LOCKS = [lock(101, "Front door"), lock(102, "Garage", state=3, door=0), lock(103, "Gate", kind=2)]


async def no_wait(_seconds):
    return None


def tools(http, timezone=None):
    return {t.name: t for t in build_nuki_tools(Nuki(http, TOKEN), timezone, wait=no_wait)}


@pytest.fixture
def api():
    with respx.mock(base_url=API_URL, assert_all_called=False) as mock:
        mock.get("/smartlock").respond(json=LOCKS)
        yield mock


async def test_lock_states_are_named_with_door_and_battery(api, ctx):
    async with httpx.AsyncClient() as http:
        result = await tools(http, "Europe/Paris")["get_lock_state"].handler({}, ctx)

    front, garage, gate = result["locks"]
    assert front == {
        "id": 101,
        "name": "Front door",
        "device": "smart_lock",
        "state": "locked",
        "door": "closed",
        "battery_critical": False,
        "battery_percent": 81,
        "updated_at": "2026-11-13T09:30:00+01:00",
    }
    assert garage["state"] == "unlocked"
    assert "door" not in garage
    assert gate["device"] == "opener"
    assert gate["state"] == "online"
    assert api.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert not ctx.account_read


async def test_the_name_filter_ignores_case_and_accents(api, ctx):
    async with httpx.AsyncClient() as http:
        result = await tools(http)["get_lock_state"].handler({"name": "frònt DOOR"}, ctx)

    assert [lock["name"] for lock in result["locks"]] == ["Front door"]


async def test_an_offline_lock_says_so(ctx):
    with respx.mock(base_url=API_URL) as mock:
        mock.get("/smartlock").respond(json=[lock(101, "Front door", server=4)])
        async with httpx.AsyncClient() as http:
            result = await tools(http)["get_lock_state"].handler({}, ctx)

    assert result["locks"][0]["connection"] == "offline"


async def test_lock_door_sends_the_lock_action(api, ctx):
    action = api.post("/smartlock/101/action").respond(204)
    async with httpx.AsyncClient() as http:
        result = await tools(http)["lock_door"].handler({"lock": "front door"}, ctx)

    assert json.loads(action.calls[0].request.content) == {"action": 2}
    assert result["sent"] == "lock"
    assert result["lock"] == "Front door"


async def test_a_lock_is_found_by_id(api, ctx):
    action = api.post("/smartlock/102/action").respond(204)
    async with httpx.AsyncClient() as http:
        await tools(http)["lock_door"].handler({"lock": "102"}, ctx)

    assert action.called


async def test_unlock_is_always_parked_with_one_french_line(api, ctx):
    action = api.post("/smartlock/101/action").respond(204)
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry(build_nuki_tools(Nuki(http, TOKEN), wait=no_wait))
        result = await registry.run("unlock_door", {"lock": "Front door"}, ctx)

    assert result["status"] == "waiting_for_the_user"
    assert not action.called
    assert ctx.actions[0]["summary"] == 'Unlock the door "Front door" (Nuki)'


async def test_unlatch_is_described_as_opening_the_door(api, ctx):
    async with httpx.AsyncClient() as http:
        tool = tools(http)["unlock_door"]
        line = await tool.summary({"lock": "Front door", "open": True})

    assert line.startswith('Open the door "Front door"')
    assert tool.always_propose and tool.needs_clean_turn


@pytest.mark.parametrize(("open_", "code", "sent"), [(False, 1, "unlock"), (True, 3, "unlatch")])
async def test_an_approved_unlock_sends_its_action(api, ctx, open_, code, sent):
    action = api.post("/smartlock/101/action").respond(204)
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry(build_nuki_tools(Nuki(http, TOKEN), wait=no_wait))
        result = await registry.run(
            "unlock_door", {"lock": "Front door", "open": open_}, ctx, approved=True
        )

    assert json.loads(action.calls[0].request.content) == {"action": code}
    assert result["sent"] == sent


async def test_lock_door_waits_for_the_go_after_a_read(api, ctx):
    action = api.post("/smartlock/101/action").respond(204)
    ctx.tainted = True
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry(build_nuki_tools(Nuki(http, TOKEN), wait=no_wait))
        result = await registry.run("lock_door", {"lock": "Front door"}, ctx)

    assert result["status"] == "waiting_for_the_user"
    assert not action.called
    assert ctx.actions[0]["summary"] == 'Lock the door "Front door" (Nuki)'


async def test_an_opener_is_never_driven(api, ctx):
    action = api.post("/smartlock/103/action").respond(204)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="only smart locks"):
            await tools(http)["lock_door"].handler({"lock": "Gate"}, ctx)

    assert not action.called


async def test_several_locks_need_a_name(api, ctx):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="Several locks"):
            await tools(http)["lock_door"].handler({}, ctx)
        with pytest.raises(ToolError, match="No lock named 'Cellar'"):
            await tools(http)["lock_door"].handler({"lock": "Cellar"}, ctx)


async def test_the_only_lock_needs_no_name(ctx):
    with respx.mock(base_url=API_URL) as mock:
        mock.get("/smartlock").respond(json=[lock(101, "Front door")])
        action = mock.post("/smartlock/101/action").respond(204)
        async with httpx.AsyncClient() as http:
            await tools(http)["lock_door"].handler({}, ctx)

    assert action.called


async def test_an_offline_lock_is_not_acted_on(ctx):
    with respx.mock(base_url=API_URL) as mock:
        mock.get("/smartlock").respond(json=[lock(101, "Front door", server=4)])
        async with httpx.AsyncClient() as http:
            with pytest.raises(ToolError, match="offline"):
                await tools(http)["lock_door"].handler({}, ctx)


async def test_activity_names_who_did_what_and_marks_the_account_read(api, ctx):
    logs = api.get("/smartlock/log").respond(
        json=[
            {
                "id": "a",
                "smartlockId": 101,
                "deviceType": 4,
                "name": "Sam",
                "action": 1,
                "trigger": 5,
                "state": 0,
                "autoUnlock": True,
                "date": "2026-11-13T07:02:11.000Z",
            },
            {
                "id": "b",
                "smartlockId": 102,
                "deviceType": 4,
                "name": "",
                "action": 2,
                "trigger": 255,
                "state": 0,
                "source": 1,
                "autoUnlock": False,
                "date": "2026-11-12T21:40:00.000Z",
            },
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http, "Europe/Paris")["get_lock_activity"].handler({"limit": 5}, ctx)

    assert result["entries"] == [
        {
            "at": "2026-11-13T08:02:11+01:00",
            "lock": "Front door",
            "action": "unlock",
            "by": "Sam",
            "trigger": "app",
            "result": "success",
            "auto_unlock": True,
        },
        {
            "at": "2026-11-12T22:40:00+01:00",
            "lock": "Garage",
            "action": "lock",
            "trigger": "keypad",
            "result": "success",
            "via": "keypad_code",
        },
    ]
    assert logs.calls[0].request.url.params["limit"] == "5"
    assert ctx.account_read


async def test_activity_of_one_lock_uses_its_log(api, ctx):
    logs = api.get("/smartlock/101/log").respond(json=[])
    async with httpx.AsyncClient() as http:
        result = await tools(http)["get_lock_activity"].handler({"lock": "Front door"}, ctx)

    assert logs.calls[0].request.url.params["limit"] == "10"
    assert result["entries"] == []


async def test_a_refused_token_says_how_to_get_a_new_one(ctx):
    with respx.mock(base_url=API_URL) as mock:
        mock.get("/smartlock").respond(401)
        async with httpx.AsyncClient() as http:
            with pytest.raises(ToolError, match=r"web\.nuki\.io > API"):
                await tools(http)["get_lock_state"].handler({}, ctx)


async def test_an_unpaid_account_is_explained(api, ctx):
    api.post("/smartlock/101/action").respond(402)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="paid subscription"):
            await tools(http)["lock_door"].handler({"lock": "Front door"}, ctx)


async def test_a_network_failure_is_a_tool_error(ctx):
    with respx.mock(base_url=API_URL) as mock:
        mock.get("/smartlock").mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ToolError, match="Nuki unavailable"):
                await tools(http)["get_lock_state"].handler({}, ctx)


async def test_with_unlock_codes_the_door_opens_on_the_users_proof_not_a_go(api, ctx, tmp_path):
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    codes = UnlockCodes(tmp_path / "codes.json", secret, clock=lambda: 59.0)
    action = api.post("/smartlock/101/action").respond(204)
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry(build_nuki_tools(Nuki(http, TOKEN), None, codes, wait=no_wait))
        asked = await registry.run("unlock_door", {"lock": "Front door"}, ctx)
        assert "6-digit code" in asked["ask"] and not action.called
        await registry.run(
            "unlock_door", {"lock": "Front door", "open": True, "code": "287082"}, ctx
        )
        assert json.loads(action.calls[0].request.content) == {"action": 3}
        ctx.tainted = True
        with pytest.raises(ToolError, match="disabled"):
            await registry.run("unlock_door", {"lock": "Front door", "code": "287082"}, ctx)


async def test_lock_door_waits_until_nuki_reports_the_lock_turned(ctx):
    unlocked = lock(101, "Front door", state=3)
    turned = {**lock(101, "Front door", state=1), "updateDate": "2026-11-14T17:23:05.000Z"}
    with respx.mock(base_url=API_URL) as mock:
        mock.get("/smartlock").mock(
            side_effect=[httpx.Response(200, json=r) for r in ([unlocked], [unlocked], [turned])]
        )
        mock.post("/smartlock/101/action").respond(204)
        async with httpx.AsyncClient() as http:
            result = await tools(http)["lock_door"].handler({}, ctx)

    assert result == {"sent": "lock", "lock": "Front door", "confirmed": True, "state": "locked"}


async def test_a_stale_state_is_not_taken_for_the_unlatch(ctx):
    # Unlocked before the command, and Nuki never updates: the unlatch is not confirmed.
    before = lock(101, "Front door", state=3)
    with respx.mock(base_url=API_URL) as mock:
        polls = mock.get("/smartlock").respond(json=[before])
        mock.post("/smartlock/101/action").respond(204)
        async with httpx.AsyncClient() as http:
            registry = ToolRegistry(build_nuki_tools(Nuki(http, TOKEN), wait=no_wait))
            result = await registry.run("unlock_door", {"open": True}, ctx, approved=True)

    assert result["confirmed"] is False
    assert result["state"] == "unlocked"
    assert "get_lock_activity" in result["note"]
    assert polls.call_count == 1 + 6


async def test_locking_a_lock_already_locked_is_confirmed_at_once(api, ctx):
    api.post("/smartlock/101/action").respond(204)
    async with httpx.AsyncClient() as http:
        result = await tools(http)["lock_door"].handler({"lock": "Front door"}, ctx)

    assert result["confirmed"] is True
