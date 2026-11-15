from types import SimpleNamespace

import pytest

from intelligent_home_agent.icloud_web import FIND_MY_RENEW, ICloudWeb
from intelligent_home_agent.tools import ToolError, ToolRegistry, build_find_my_tools, find_my
from intelligent_home_agent.tools.find_my import FindMy, device_from

HOME = (0.0, 0.0)
IPHONE = {
    "id": "phone-1",
    "name": "Sam's iPhone",
    "deviceDisplayName": "iPhone 15",
    "deviceStatus": "200",
    "batteryLevel": 0.84,
    "batteryStatus": "NotCharging",
    "lowPowerMode": False,
    "location": {
        "latitude": 0.00001,
        "longitude": -0.00002,
        "horizontalAccuracy": 9.4,
        "timeStamp": 1792844911957,
    },
}
WATCH = {
    "id": "watch-1",
    "name": "Sam’s Apple Watch",
    "deviceDisplayName": "Apple Watch SE",
    "deviceStatus": "203",
    "batteryLevel": 0.0,
    "batteryStatus": "Unknown",
}


def test_unknown_battery_is_none_not_zero():
    assert device_from(IPHONE, True).battery == 84
    watch = device_from(WATCH, False)
    assert watch.battery is None and watch.status == "offline" and watch.latitude is None
    flat = device_from({**WATCH, "batteryStatus": "Charging"}, False)
    assert flat.battery == 0 and flat.charging == "charging"


class FakeDevice:
    def __init__(self, data):
        self.data = data
        self.sound_available = True
        self.rang = False

    def play_sound(self):
        self.rang = True


class FakeManager(list):
    def __init__(self, devices):
        super().__init__(devices)
        self.refreshes = []
        self.user_info = {"membersInfo": {"prs-jo": {"firstName": "JO"}}}

    def refresh(self, locate):
        self.refreshes.append(locate)


FAMILY_PHONE = {**IPHONE, "id": "jo-1", "name": "Jo (2)", "prsId": "prs-jo"}


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(find_my, "LOCATE_WAIT_SECONDS", 0)
    return FakeManager([FakeDevice(IPHONE), FakeDevice(WATCH), FakeDevice(FAMILY_PHONE)])


def tools_for(manager):
    api = SimpleNamespace(
        devices=manager, data={"ok": 1}, requires_2fa=False, is_trusted_session=True
    )
    source = FindMy(ICloudWeb("sam@example.com", "unused", opener=lambda *_: api))
    return build_find_my_tools(source, "Europe/Rome", HOME)


async def test_devices_are_located_now_and_placed_against_home(ctx, manager):
    devices, _ = tools_for(manager)

    result = await devices.handler({"name": "iphone"}, ctx)

    assert manager.refreshes == [True, False]
    (phone,) = result["devices"]
    assert phone["battery_percent"] == 84 and phone["status"] == "online"
    assert phone["location"]["at_home"] is True and phone["location"]["km_from_home"] == 0.0
    assert phone["location"]["map"] == "https://maps.apple.com/?ll=0.00001,-0.00002"
    assert phone["owner"] == "the user"
    assert not devices.taints and not devices.needs_clean_turn


async def test_family_devices_on_request_with_their_owner(ctx, manager):
    devices, ring = tools_for(manager)

    mine = await devices.handler({}, ctx)
    family = await devices.handler({"family": True, "name": "jo"}, ctx)

    assert {d["id"] for d in mine["devices"]} == {"phone-1", "watch-1"}
    (phone,) = family["devices"]
    assert phone["owner"] == "Jo" and phone["model"] == "iPhone 15"
    assert await ring.summary({"id": "jo-1"}) == ('Play a sound on "Jo (2)" (iPhone 15, Jo\'s)')


async def test_a_location_read_opens_no_new_site_but_ringing_still_runs(ctx, manager):
    devices, ring = tools_for(manager)

    await devices.handler({"name": "iphone"}, ctx)

    assert ctx.account_read and not ctx.tainted
    registry = ToolRegistry([devices, ring])
    result = await registry.run("play_sound_on_device", {"id": "phone-1"}, ctx)
    assert result["status"] == "ringing"


async def test_ringing_waits_after_a_read_and_names_the_device(ctx, manager):
    devices, ring = tools_for(manager)
    assert ring.needs_clean_turn and ring.proposable
    with pytest.raises(ToolError, match="find_my_devices"):
        await ring.summary({"id": "phone-1"})

    await devices.handler({}, ctx)
    assert await ring.summary({"id": "phone-1"}) == ('Play a sound on "Sam\'s iPhone" (iPhone 15)')
    result = await ring.handler({"id": "phone-1"}, ctx)

    assert result["status"] == "ringing" and manager[0].rang and not manager[1].rang
    assert result["owner"] == "the user"
    with pytest.raises(ToolError, match="find_my_devices"):
        await ring.handler({"id": "nope"}, ctx)


async def test_find_my_asking_for_a_fresh_login_leaves_the_session_to_the_other_apps(ctx):
    from pyicloud.exceptions import PyiCloudAuthRequiredException

    class Api(SimpleNamespace):
        @property
        def devices(self):
            raise PyiCloudAuthRequiredException("sam@example.com", None)

    opened = []

    def opener(*_):
        opened.append(1)
        return Api(data={"ok": 1}, requires_2fa=False, is_trusted_session=True)

    icloud = ICloudWeb("sam@example.com", "unused", opener=opener)
    devices, _ = build_find_my_tools(FindMy(icloud), "Europe/Rome", HOME)

    for _ in range(2):
        with pytest.raises(ToolError) as caught:
            await devices.handler({}, ctx)
        assert str(caught.value) == FIND_MY_RENEW
    assert opened == [1]
