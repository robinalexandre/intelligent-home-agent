"""The car's tools (tools/car.py) and their first provider, an Audi through myAudi
(tools/audi.py): what is read, what waits, what asks for proof, what counts as done."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from intelligent_home_agent import main
from intelligent_home_agent.config import Settings
from intelligent_home_agent.tools.audi import BFF_URL, TOKEN_URL, Audi, AudiAuth
from intelligent_home_agent.tools.base import ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.car import (
    HEATING,
    LOCATION,
    LOCK,
    PENDING,
    SENT,
    UNLOCK,
    Car,
    CarAccount,
    build_car,
    build_car_tools,
    providers,
)
from intelligent_home_agent.tools.proposals import build_proposal_tools
from intelligent_home_agent.tools.unlock_codes import UnlockCodes, totp

VIN = "WAUZZZF40KA000001"
CAR = f"{BFF_URL}/vehicle/v1/vehicles/{VIN}"
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
CAR_TOOLS = {
    "get_car_state",
    "get_car_location",
    "lock_car",
    "unlock_car",
    "start_car_heating",
    "stop_car_heating",
}

STATUS = {
    "access": {
        "accessStatus": {
            "value": {
                "carCapturedTimestamp": "2026-11-14T07:12:00Z",
                "doorLockStatus": "unlocked",
                "doors": [
                    {"name": "frontLeft", "status": ["unlocked", "open"]},
                    {"name": "trunk", "status": ["unlocked", "closed"]},
                ],
                "windows": [
                    {"name": "frontLeft", "status": ["closed"]},
                    {"name": "sunRoof", "status": ["unsupported"]},
                ],
            }
        }
    },
    "fuelStatus": {
        "rangeStatus": {
            "value": {
                "carCapturedTimestamp": "2026-11-14T07:12:00Z",
                "carType": "diesel",
                "primaryEngine": {"type": "diesel", "remainingRange_km": 612},
                "totalRange_km": 612,
            }
        }
    },
    "measurements": {
        "fuelLevelStatus": {"value": {"currentFuelLevel_pct": 55}},
        "odometerStatus": {"value": {"odometer": 84210}},
        "rangeStatus": {"value": {"adBlueRange": 4000}},
    },
    "vehicleHealthInspection": {
        "maintenanceStatus": {
            "value": {
                "inspectionDue_days": 120,
                "inspectionDue_km": 9000,
                "oilServiceDue_days": 60,
                "oilServiceDue_km": 4500,
            }
        }
    },
    "vehicleLights": {"lightsStatus": {"value": {"lights": [{"name": "left", "status": "off"}]}}},
    "readiness": {"readinessStatus": {"value": {"connectionState": {"isOnline": True}}}},
    "userCapabilities": {
        "capabilitiesStatus": {"value": [{"id": "access"}, {"id": "auxiliaryHeating"}]}
    },
    "oilLevel": {"error": {"message": "not supported"}},
}


class Clock:
    def __init__(self, now=59.0):
        self.now = now

    def __call__(self):
        return self.now


async def no_sleep(_):
    return None


def auth(http, tmp_path, token="refresh-1"):  # noqa: S107 - a test value
    return AudiAuth(http, "client", token, tmp_path / "car.json")


def tools(http, tmp_path, spin="1234", codes=None, language="en", name="the Audi"):
    audi = Audi(http, auth(http, tmp_path), VIN, spin, sleep=no_sleep)
    built = build_car_tools(audi, name, codes, "Europe/Paris", language=language)
    return {t.name: t for t in built}


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(
            json={"access_token": "access", "expires_in": 3600, "refresh_token": "refresh-2"}
        )
        yield mock


async def test_the_rotated_refresh_token_is_kept_for_the_next_start(api, tmp_path):
    async with httpx.AsyncClient() as http:
        assert await auth(http, tmp_path).access_token() == "access"
        restarted = auth(http, tmp_path)
    assert restarted.refresh_token == "refresh-2"
    assert (tmp_path / "car.json").stat().st_mode & 0o777 == 0o600
    sent = parse_qs(api.calls.last.request.content.decode())
    assert sent["refresh_token"] == ["refresh-1"] and sent["audience"] == ["https://api.cariad.com"]


async def test_a_new_login_wins_over_the_saved_token(api, tmp_path):
    async with httpx.AsyncClient() as http:
        await auth(http, tmp_path).access_token()
        relogged = auth(http, tmp_path, token="fresh-login")
    assert relogged.refresh_token == "fresh-login"


async def test_a_refused_refresh_says_how_to_log_in_again(tmp_path):
    with respx.mock() as mock:
        mock.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        async with httpx.AsyncClient() as http:
            with pytest.raises(ToolError, match="audi_login.py"):
                await auth(http, tmp_path).access_token()


async def test_state_reads_lock_doors_range_and_service(api, tmp_path, ctx):
    api.get(url__startswith=f"{CAR}/selectivestatus").respond(207, json=STATUS)
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["get_car_state"].handler({}, ctx)
    assert result == {
        "car": "the Audi",
        "locked": False,
        "open_doors": ["frontLeft"],
        "range_km": 612,
        "fuel_percent": 55,
        "adblue_range_km": 4000,
        "odometer_km": 84210,
        "inspection_due_days": 120,
        "inspection_due_km": 9000,
        "oil_service_due_days": 60,
        "oil_service_due_km": 4500,
        "online": True,
        "captured_at": "2026-11-14T08:12:00+01:00",
        "services": ["access", "auxiliaryHeating"],
    }
    assert api.calls.last.request.headers["Authorization"] == "Bearer access"


async def test_location_while_driving_and_parked(api, tmp_path, ctx):
    route = api.get(f"{CAR}/parkingposition")
    route.respond(204)
    async with httpx.AsyncClient() as http:
        car = tools(http, tmp_path)
        assert (await car["get_car_location"].handler({}, ctx))["parked"] is False
        # Even an empty answer was a read of the user's account.
        assert ctx.account_read
        route.respond(
            json={
                "data": {
                    "lat": 48.8584,
                    "lon": 2.2945,
                    "carCapturedTimestamp": "2026-11-14T07:00:00Z",
                }
            }
        )
        fresh = ToolContext(conversation=ctx.conversation)
        parked = await car["get_car_location"].handler({}, fresh)
    assert parked["latitude"] == 48.8584 and parked["longitude"] == 2.2945
    assert parked["captured_at"] == "2026-11-14T08:00:00+01:00"
    assert "mlat=48.8584" in parked["map"]
    # Where the car is parked is the user's own: no new site opens after it.
    assert fresh.account_read


async def test_lock_waits_for_the_car_to_confirm(api, tmp_path, ctx):
    lock = api.post(f"{CAR}/access/lock").respond(202, json={"data": {"requestID": "r1"}})
    api.get(f"{CAR}/pendingrequests").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"id": "r1", "status": "in_progress"}]}),
            httpx.Response(200, json={"data": [{"id": "r1", "status": "successful"}]}),
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["lock_car"].handler({}, ctx)
    assert result["result"] == "succeeded" and "confirms" in result["note"]
    assert json.loads(lock.calls.last.request.content) == {"spin": "1234"}


async def test_a_command_the_car_never_confirmed_is_not_reported_done(api, tmp_path, ctx):
    api.post(f"{CAR}/access/lock").respond(202, json={"data": {"requestID": "r1"}})
    api.get(f"{CAR}/pendingrequests").respond(
        json={"data": [{"id": "r1", "status": "in_progress"}]}
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http, tmp_path)["lock_car"].handler({}, ctx)
    assert result["result"] == PENDING and "don't say it is done" in result["note"]


async def test_a_refused_command_says_so(api, tmp_path, ctx):
    api.post(f"{CAR}/access/lock").respond(202, json={"data": {"requestID": "r1"}})
    api.get(f"{CAR}/pendingrequests").respond(json={"data": [{"id": "r1", "status": "failed"}]})
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="refused the lock"):
            await tools(http, tmp_path)["lock_car"].handler({}, ctx)


async def test_a_wrong_spin_or_missing_service_is_explained(api, tmp_path, ctx):
    api.post(f"{CAR}/auxiliaryheating/start").respond(403)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="S-PIN"):
            await tools(http, tmp_path)["start_car_heating"].handler({}, ctx)


async def test_the_heating_runs_within_bounds(api, tmp_path, ctx):
    start = api.post(f"{CAR}/auxiliaryheating/start").respond(204)
    stop = api.post(f"{CAR}/auxiliaryheating/stop").respond(204)
    async with httpx.AsyncClient() as http:
        car = tools(http, tmp_path)
        result = await car["start_car_heating"].handler({"minutes": 90}, ctx)
        line = await car["start_car_heating"].describe({"minutes": 90})
        french = tools(http, tmp_path, language="fr", name="l'Audi")
        ligne = await french["start_car_heating"].describe({})
        stopped = await car["stop_car_heating"].handler({}, ctx)
        unnamed = tools(http, tmp_path, name="")
        assert await unnamed["lock_car"].describe({}) == "Lock the car"
        assert await car["stop_car_heating"].describe({}) == "Stop the heating of the Audi"
    # Without a confirmation number it was only sent, and says so.
    assert result["minutes"] == 60 and result["result"] == SENT
    assert "don't say it is done" in result["note"]
    assert json.loads(start.calls.last.request.content) == {"duration_min": 60, "spin": "1234"}
    assert line == "Start the heating of the Audi for 60 min"
    assert ligne == "Allumer le chauffage de l'Audi pendant 30 min"
    assert stop.called and stopped["action"] == "heating_stop"


async def test_without_the_pin_only_reading_tools(api, tmp_path):
    async with httpx.AsyncClient() as http:
        assert set(tools(http, tmp_path, spin=None)) == {"get_car_state", "get_car_location"}
        # With the PIN but no unlock codes: everything but unlocking.
        assert set(tools(http, tmp_path)) == CAR_TOOLS - {"unlock_car"}
        codes = UnlockCodes(tmp_path / "codes.json", SECRET, clock=Clock())
        assert set(tools(http, tmp_path, codes=codes)) == CAR_TOOLS
        # Unlock codes alone never make an unlock_car.
        assert "unlock_car" not in tools(http, tmp_path, spin=None, codes=codes)


async def test_unlock_asks_for_proof_then_opens(api, tmp_path, ctx):
    unlock = api.post(f"{CAR}/access/unlock").respond(204)
    codes = UnlockCodes(tmp_path / "codes.json", SECRET, clock=Clock())
    async with httpx.AsyncClient() as http:
        car = tools(http, tmp_path, codes=codes)
        asked = await car["unlock_car"].handler({}, ctx)
        assert "6-digit code" in asked["ask"] and not unlock.called
        with pytest.raises(ToolError, match="Wrong"):
            await car["unlock_car"].handler({"code": "000000"}, ctx)
        assert not unlock.called
        await car["unlock_car"].handler({"code": totp(SECRET, 1)}, ctx)
        assert unlock.call_count == 1
        # A code works once.
        with pytest.raises(ToolError):
            await car["unlock_car"].handler({"code": totp(SECRET, 1)}, ctx)
    assert unlock.call_count == 1


async def test_unlock_is_refused_not_parked_after_reading_others_content(api, tmp_path, ctx):
    codes = UnlockCodes(tmp_path / "codes.json", SECRET, clock=Clock())
    unlock = api.post(f"{CAR}/access/unlock").respond(204)
    lock = api.post(f"{CAR}/access/lock").respond(204)
    heat = api.post(f"{CAR}/auxiliaryheating/start").respond(204)
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry(list(tools(http, tmp_path, codes=codes).values()))
        registry.extend(build_proposal_tools(registry))
        ctx.tainted = True
        with pytest.raises(ToolError, match="disabled"):
            await registry.run("unlock_car", {"code": totp(SECRET, 1)}, ctx)
        assert not ctx.conversation.proposals and not unlock.called
        parked = await registry.run("lock_car", {}, ctx)
        heating = await registry.run("start_car_heating", {"minutes": 20}, ctx)
        assert parked["status"] == heating["status"] == "waiting_for_the_user"
        assert not lock.called and not heat.called
        assert [a["summary"] for a in ctx.actions] == [
            "Lock the Audi",
            "Start the heating of the Audi for 20 min",
        ]
        # The user's yes, in a later message that read nothing, runs them.
        for proposal in ctx.conversation.proposals:
            proposal.shown = True
        ctx.conversation.turn += 1
        await registry.run("run_proposals", {}, ToolContext(conversation=ctx.conversation))
    assert lock.call_count == 1 and heat.call_count == 1 and not unlock.called


# --- The interface: any make ----------------------------------------------------------------


class ReadOnlyCar(Car):
    """A make whose cloud only tells the car's state."""

    vendor = "Example Motors"

    async def state(self):
        return {"locked": True, "battery_percent": 80, "captured_at": "2026-11-14T07:12:00Z"}


class FullCar(ReadOnlyCar):
    heating_minutes = (5, 20)

    def __init__(self):
        self.done: list[str] = []

    def can(self, capability):
        return capability in (LOCATION, LOCK, UNLOCK, HEATING)

    async def location(self):
        return None

    async def lock(self):
        self.done.append("lock")
        return PENDING

    async def start_heating(self, minutes):
        self.done.append(f"heat {minutes}")
        return SENT


async def test_a_provider_only_gets_the_tools_it_can_serve(tmp_path, ctx):
    assert build_car_tools(None) == []
    only = {t.name: t for t in build_car_tools(ReadOnlyCar())}
    assert set(only) == {"get_car_state"}
    assert await only["get_car_state"].handler({}, ctx) == {
        "car": "the car",
        "locked": True,
        "battery_percent": 80,
        "captured_at": "2026-11-14T07:12:00Z",
    }
    codes = UnlockCodes(tmp_path / "codes.json", SECRET, clock=Clock())
    car = FullCar()
    full = {t.name: t for t in build_car_tools(car, "", codes)}
    assert set(full) == CAR_TOOLS
    # The rules come with the tools, whatever the make.
    for name in ("lock_car", "start_car_heating", "stop_car_heating"):
        assert full[name].needs_clean_turn and full[name].describe is not None
        assert not full[name].never_park
    assert full["unlock_car"].needs_clean_turn and full["unlock_car"].never_park
    assert "Example Motors" in full["get_car_state"].description
    heated = await full["start_car_heating"].handler({"minutes": 45}, ctx)
    assert heated["minutes"] == 20 and car.done == ["heat 20"]
    assert (await full["lock_car"].handler({}, ctx))["result"] == PENDING
    assert (await full["get_car_location"].handler({}, ctx))["parked"] is False
    assert ctx.account_read


def settings(monkeypatch, tmp_path, **env) -> Settings:
    for key in ("CAR_PROVIDER", "CAR_VIN", "CAR_REFRESH_TOKEN", "CAR_PIN", "UNLOCK_TOTP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_API_TOKEN", "test-token")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_the_car_is_off_until_its_provider_and_login_are_set(monkeypatch, tmp_path):
    assert set(providers()) == set(Settings.model_fields["car_provider"].annotation.__args__) - {""}
    assert build_car("", None, None) is None

    def names(**env):
        conf = settings(monkeypatch, tmp_path, **env)
        return main.build_registry(conf, ha=None, http=None, llm=None).names() & CAR_TOOLS

    login = {"CAR_VIN": VIN, "CAR_REFRESH_TOKEN": "refresh-1"}
    assert names() == set()
    assert names(**login) == set()
    assert names(CAR_PROVIDER="audi") == set()
    assert names(CAR_PROVIDER="audi", **login) == {"get_car_state", "get_car_location"}
    with_pin = names(CAR_PROVIDER="audi", CAR_PIN="1234", **login)
    assert with_pin == CAR_TOOLS - {"unlock_car"}
    assert names(CAR_PROVIDER="audi", CAR_PIN="1234", UNLOCK_TOTP_SECRET=SECRET, **login) == (
        CAR_TOOLS
    )
    with pytest.raises(ValueError):
        settings(monkeypatch, tmp_path, CAR_PROVIDER="another-make")
    account = CarAccount(vin=VIN, refresh_token="r", store=tmp_path / "car.json")
    assert isinstance(build_car("audi", None, account), Audi)
