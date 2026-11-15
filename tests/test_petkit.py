import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("pypetkitapi")  # the "petkit" extra

from pypetkitapi import Feeder, Litter, PetKitClient, PetkitSessionExpiredError  # noqa: E402

from intelligent_home_agent.tools import ToolError  # noqa: E402
from intelligent_home_agent.tools.petkit import (  # noqa: E402
    PetKit,
    build_petkit_tools,
    feeder_status,
    litter_status,
)

TZ = "Europe/Paris"
PARIS = ZoneInfo(TZ)


def nfo(device_id: int, device_type: str, name: str) -> dict[str, Any]:
    return {
        "createdAt": 0,
        "deviceId": device_id,
        "deviceName": name,
        "deviceType": device_type,
        "groupId": 1,
        "type": 0,
        "uniqueId": str(device_id),
    }


def feeder(food=1, items=None, device_type="d4") -> Feeder:
    return Feeder.model_validate(
        {
            "id": 11,
            "name": "Petkit_D4",
            "sn": "SN1",
            "firmware": "1.0",
            "hardware": 1,
            "state": {
                "food": food,
                "feeding": 0,
                "desiccantLeftDays": 12,
                "feedState": {"realAmountTotal": 40, "addAmountTotal": 10},
            },
            "device_records": {"feed": [{"day": 20261113, "items": items or []}]},
            "device_nfo": nfo(11, device_type, "Petkit_D4"),
        }
    )


def litter(records=None, work_state=None, stats=None) -> Litter:
    state: dict[str, Any] = {"sandPercent": 64, "sandLack": False, "boxFull": True}
    if work_state:
        state["workState"] = work_state
    return Litter.model_validate(
        {
            "id": 22,
            "name": "Petkit_T4",
            "sn": "SN2",
            "hardware": 1,
            "firmwareDetails": [],
            "state": state,
            "device_records": records or [],
            "device_stats": stats,
            "device_nfo": nfo(22, "t4", "Petkit_T4"),
        }
    )


def at(hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, 13, hour, minute, tzinfo=PARIS).timestamp())


MEALS = [
    {"time": 8 * 3600, "amount": 20, "src": 1, "state": {"realAmount": 20}},
    {"time": 12 * 3600 + 30 * 60, "amount": 10, "src": 4, "state": {"realAmount": 10}},
    {"time": 19 * 3600, "amount": 20, "src": 1},
    {"time": 7 * 3600, "amount": 20, "src": 1, "state": {"realAmount": 15, "errMsg": "jam"}},
]


class FakeClient:
    def __init__(self, entities, fail_first=None) -> None:
        self.petkit_entities = {e.id: e for e in entities}
        self.fail_first = fail_first
        self.fetches = 0
        self.sent: list[tuple[int, Any, Any]] = []
        self._session: object | None = object()

    async def get_devices_data(self) -> None:
        self.fetches += 1
        if self.fail_first is not None and self.fetches == 1:
            raise self.fail_first

    async def send_api_request(self, device_id, action, setting=None) -> bool:
        self.sent.append((device_id, action, setting))
        return True


def petkit_with(client) -> PetKit:
    async def factory():
        return client

    return PetKit("pets@example.com", "pw", "GB", TZ, client_factory=factory)


def tools(client) -> dict[str, Any]:
    return {t.name: t for t in build_petkit_tools(petkit_with(client))}


def test_feeder_status_lists_todays_meals_and_the_next_one():
    status = feeder_status(feeder(items=MEALS), datetime(2026, 11, 13, 15, 0, tzinfo=PARIS))

    assert status["name"] == "Petkit_D4"
    assert status["unit"] == "g"
    assert status["food_low"] is False
    assert status["desiccant_days_left"] == 12
    assert status["dispensed_today"] == 40
    assert status["meals_today"] == [
        {"at": "07:00", "amount": 20, "dispensed": 15, "error": "jam"},
        {"at": "08:00", "amount": 20, "dispensed": 20},
        {"at": "12:30", "amount": 10, "dispensed": 10, "manual": True},
    ]
    assert status["next_meal"] == {"at": "19:00", "amount": 20}


def test_an_empty_hopper_is_low_food_and_no_meal_left_is_said():
    status = feeder_status(feeder(food=0, items=MEALS[:2]), datetime(2026, 11, 13, 21, 0))
    assert status["food_low"] is True
    assert status["next_meal"] == "none left today"


def test_litter_status_reads_visits_and_the_last_cleaning():
    records = [
        {
            "eventType": 10,
            "timestamp": at(9, 5),
            "petName": "Pixel",
            "content": {"timeIn": 100, "timeOut": 160},
            "subContent": [
                {"eventType": 5, "timestamp": at(9, 10), "content": {"result": 0, "startReason": 0}}
            ],
        },
        {"eventType": 10, "timestamp": at(14, 0), "petName": "Pixel"},
        {"eventType": 5, "timestamp": at(16, 0), "content": {"result": 0}},
    ]
    status = litter_status(litter(records, stats={"times": 2}), PARIS)

    assert status["litter_percent"] == 64
    assert status["waste_bin_full"] is True
    assert status["working_now"] is False
    assert status["uses_today"] == 2
    assert status["last_uses_today"] == [
        {"at": "2026-11-13T09:05+01:00", "pet": "Pixel", "seconds": 60},
        {"at": "2026-11-13T14:00+01:00", "pet": "Pixel"},
    ]
    assert status["last_cleaning_today"]["at"] == "2026-11-13T16:00+01:00"


async def test_status_tool_marks_the_request_as_account_read(ctx):
    client = FakeClient([feeder(items=MEALS), litter()])
    result = await tools(client)["get_pet_devices_status"].handler({}, ctx)

    assert [f["name"] for f in result["feeders"]] == ["Petkit_D4"]
    assert [lb["name"] for lb in result["litter_boxes"]] == ["Petkit_T4"]
    assert ctx.account_read


async def test_an_expired_session_is_retried_once_with_a_fresh_login(ctx):
    client = FakeClient([feeder(), litter()], fail_first=PetkitSessionExpiredError("expired"))
    await tools(client)["get_pet_devices_status"].handler({}, ctx)
    assert client.fetches == 2


async def test_a_session_dropped_during_the_fetch_fetches_again(ctx):
    client = FakeClient([feeder(), litter()])

    async def drops_once() -> None:
        client.fetches += 1
        client._session = None if client.fetches == 1 else object()

    client.get_devices_data = drops_once
    await tools(client)["get_pet_devices_status"].handler({}, ctx)
    assert client.fetches == 2


async def test_a_session_that_keeps_expiring_tells_to_use_a_second_account(ctx):
    client = FakeClient([feeder()])
    client.get_devices_data = AsyncMock(side_effect=PetkitSessionExpiredError("expired"))
    with pytest.raises(ToolError, match="second PetKit account"):
        await tools(client)["get_pet_devices_status"].handler({}, ctx)


def real_client(entities) -> tuple[PetKitClient, AsyncMock]:
    """The library's own command building, with only its HTTP layer replaced."""
    client = PetKitClient("pets@example.com", "pw", "GB", TZ, session=None)
    client.petkit_entities = {e.id: e for e in entities}
    client.get_devices_data = AsyncMock()
    client.get_session_id = AsyncMock(return_value={"X-Session": "s"})
    request = AsyncMock(return_value={})
    client.req.request = request
    return client, request


async def test_feeding_sends_grams_for_a_d4(ctx):
    client, request = real_client([feeder(), litter()])
    result = await tools(client)["feed_pet_now"].handler({"portions": 2}, ctx)

    assert result == {
        "done": True,
        "feeder": "Petkit_D4",
        "portions": 2,
        "amount": 20,
        "unit": "g",
    }
    call = request.await_args.kwargs
    assert call["url"].startswith("d4/")
    assert call["data"]["amount"] == 20
    assert call["data"]["deviceId"] == 11


async def test_cleaning_starts_the_litter_box_cycle(ctx):
    client, request = real_client([feeder(), litter()])
    result = await tools(client)["clean_litter_box_now"].handler({}, ctx)

    assert result["litter_box"] == "Petkit_T4"
    call = request.await_args.kwargs
    assert call["url"] == "t4/controlDevice"
    assert json.loads(call["data"]["kv"]) == {"start_action": 0}
    assert call["data"]["type"] == "start"


async def test_cleaning_is_refused_while_the_box_works(ctx):
    client = FakeClient([litter(work_state={"workMode": 0, "workProcess": 10})])
    with pytest.raises(ToolError, match="already working"):
        await tools(client)["clean_litter_box_now"].handler({}, ctx)
    assert client.sent == []


async def test_portions_are_capped_before_anything_is_sent(ctx):
    client = FakeClient([feeder()])
    feed = tools(client)["feed_pet_now"]
    with pytest.raises(ToolError, match="Between 1 and 3"):
        feed.vet({"portions": 5}, ctx)
    with pytest.raises(ToolError):
        await feed.handler({"portions": 0}, ctx)
    assert client.sent == []


async def test_a_named_device_must_exist(ctx):
    client = FakeClient([feeder()])
    with pytest.raises(ToolError, match="Petkit_D4"):
        await tools(client)["feed_pet_now"].handler({"portions": 1, "device": "Kitchen"}, ctx)


async def test_writes_need_a_clean_request_and_say_what_they_do():
    t = tools(FakeClient([feeder(), litter()]))
    feed, clean = t["feed_pet_now"], t["clean_litter_box_now"]

    assert feed.needs_clean_turn and clean.needs_clean_turn
    assert not t["get_pet_devices_status"].needs_clean_turn
    assert await feed.summary({"portions": 2}) == (
        "PetKit feeder: dispense 2 portion(s) of food now, on top of the planned meals"
    )
    assert "cleaning cycle" in await clean.summary({})
