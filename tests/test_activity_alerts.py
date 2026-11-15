import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from intelligent_home_agent.activity_alerts import (
    ActivityAlerts,
    Conditions,
    build_activity_alert_tools,
)
from intelligent_home_agent.config import SurfSpot, WindSpot
from intelligent_home_agent.tools import ToolError, ToolRegistry
from intelligent_home_agent.tools.weather import (
    FORECAST_URL,
    GEOCODING_URL,
    MARINE_URL,
    OpenMeteo,
)

START = date(2026, 10, 14)  # a Wednesday
BEACH = WindSpot(name="North Beach", latitude=0.0, longitude=0.0, directions=["W"], min_knots=12)
LAKE = WindSpot(name="Lake", latitude=1.0, longitude=1.0, active_from="2027-01-01")
BAY = SurfSpot(name="South Bay", latitude=2.0, longitude=2.0, facing="W")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


class Sky:
    """What Open-Meteo answers: per day (0 is the 14th) and hour, set by the test."""

    def __init__(self):
        self.wind_knots: dict[tuple[int, int], float] = {}
        self.rain: dict[tuple[int, int], int] = {}
        self.waves: dict[tuple[int, int], float] = {}
        self.calls = 0
        self.last = None

    def _series(self, days, values, default):
        return [values.get((d, h), default) for d in range(days) for h in range(24)]

    def forecast(self, request):
        self.calls += 1
        self.last = request
        days = int(request.url.params["forecast_days"])
        stamps = [START + timedelta(days=d) for d in range(days)]
        wind = self._series(days, self.wind_knots, 5)
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [f"{d}T{h:02d}:00" for d in stamps for h in range(24)],
                    "wind_speed_10m": wind,
                    "wind_gusts_10m": [w + 6 for w in wind],
                    "wind_direction_10m": [270] * 24 * days,
                    "temperature_2m": [15] * 24 * days,
                    "precipitation_probability": self._series(days, self.rain, 0),
                    "uv_index": [3] * 24 * days,
                    "snowfall": [0] * 24 * days,
                    "visibility": [20000] * 24 * days,
                },
                "daily": {
                    "sunrise": [f"{d}T08:00" for d in stamps],
                    "sunset": [f"{d}T19:30" for d in stamps],
                },
            },
        )

    def marine(self, request):
        days = int(request.url.params["forecast_days"])
        stamps = [START + timedelta(days=d) for d in range(days)]
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [f"{d}T{h:02d}:00" for d in stamps for h in range(24)],
                    "wave_height": self._series(days, self.waves, 0.2),
                    "wave_period": [11] * 24 * days,
                    "wave_direction": [270] * 24 * days,
                    "sea_level_height_msl": [0.0] * 24 * days,
                }
            },
        )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sky():
    with respx.mock(assert_all_called=False) as mock:
        sky = Sky()
        mock.get(FORECAST_URL).mock(side_effect=sky.forecast)
        mock.get(MARINE_URL).mock(side_effect=sky.marine)
        mock.get(GEOCODING_URL).respond(
            json={"results": [{"name": "Hilltown", "latitude": 5.0, "longitude": 6.0}]}
        )
        yield sky


@pytest.fixture
async def make(tmp_path, clock, sky, monkeypatch):
    async with httpx.AsyncClient() as http:
        meteo = OpenMeteo(http, "UTC")
        monkeypatch.setattr(meteo, "now_hour", lambda: clock.now.strftime("%Y-%m-%dT%H:00"))

        def build(*, home=(0.0, 0.0), live=None, language="en", wind=(BEACH, LAKE), surf=(BAY,)):
            conditions = Conditions(
                meteo,
                wind_spots=list(wind),
                surf_spots=list(surf),
                model="best_match",
                home=lambda: home,
                live=live,
            )
            return ActivityAlerts(
                tmp_path / "alerts.json", conditions, "UTC", language=language, clock=clock
            )

        yield build


class Sent(list):
    async def __call__(self, text):
        self.append(text)
        return "ntfy"


async def test_a_wind_window_tomorrow_is_announced_once_then_the_next_good_day(make, sky, clock):
    alerts, sent = make(), Sent()
    alert = alerts.add("wing", spot="north")
    assert (alert.activity, alert.spot, alert.lead_hours) == ("wingfoil", "North Beach", 24)

    assert await alerts.check(sent) == []  # 5 knots everywhere: silence
    for hour in range(14, 18):
        sky.wind_knots[(1, hour)] = 16
    sky.wind_knots[(1, 9)] = 20  # one hour alone is no session
    assert await alerts.check(sent) == []  # the forecast is read once an hour
    clock.now += timedelta(hours=1)
    assert await alerts.check(sent) == []  # tomorrow 14:00 is more than 24 hours away
    clock.now += timedelta(hours=4)
    assert await alerts.check(sent) == [alert.id]
    assert sent == [
        "🌬️ Wingfoil · North Beach, tomorrow 14:00–18:00: 16 kn W, gusts 22 kn. (alert no. 1)"
    ]
    clock.now += timedelta(hours=2)
    assert await alerts.check(sent) == [] and len(sent) == 1  # once per good day

    # The day itself: nothing more about it; the day after is announced in its turn.
    clock.now += timedelta(days=1)
    for hour in range(10, 13):
        sky.wind_knots[(2, hour)] = 18
    assert await alerts.check(sent) == [alert.id]
    assert "tomorrow 10:00–13:00: 18 kn W" in sent[1]


async def test_any_spot_means_those_in_season_and_a_threshold_of_the_alert_wins(make, sky):
    alerts, sent = make(), Sent()
    alerts.add("kitesurf", min_knots=20, lead_hours=48)
    for hour in range(10, 16):
        sky.wind_knots[(0, hour)] = 16  # enough for the spot, not for this alert
    assert await alerts.check(sent) == []
    request = sky.last
    assert request.url.params["latitude"] == "0.0"  # the lake is next year's spot


async def test_surf_is_judged_per_beach_and_fair_windows_can_be_skipped(make, sky, clock):
    alerts, sent = make(), Sent()
    alerts.add("surf", lead_hours=12)
    picky = alerts.add("surf", spot="South Bay", good_only=True, lead_hours=12)
    for hour in range(10, 14):
        sky.waves[(0, hour)] = 1.4
        sky.wind_knots[(0, hour)] = 10  # westerly on a beach facing west would be onshore
    assert await alerts.check(sent) == []
    clock.now += timedelta(hours=1)
    for hour in range(10, 14):
        sky.wind_knots[(0, hour)] = 3  # glassy
    assert await alerts.check(sent) == [1, picky.id]
    assert (
        sent[0] == "🌊 Surf · South Bay, today 10:00–14:00: waves 1.4 m, period 11 s. (alert no. 1)"
    )


async def test_a_weather_activity_gets_its_dry_window_at_home_or_in_a_place(make, sky, clock):
    alerts, sent = make(language="fr"), Sent()
    tools = {t.name: t for t in build_activity_alert_tools(alerts, language="fr")}
    for day in (0, 1):
        for hour in range(24):
            sky.rain[(day, hour)] = 90
    home = alerts.add("vélo")
    created = await tools["set_activity_alert"].handler(
        {"activity": "hiking", "place": "hilltown", "lead_hours": 36, "min_hours": 3}, None
    )
    assert created["created"] == {
        "id": 2,
        "activity": "hiking",
        "when": "forecast",
        "place": "Hilltown",
        "told_up_to_hours_before": 36,
        "window_of_at_least_hours": 3,
        "state": "on",
    }
    assert await alerts.check(sent) == []  # rain all day: silence

    clock.now += timedelta(hours=1)
    for hour in range(13, 17):
        sky.rain[(1, hour)] = 10
    assert await alerts.check(sent) == [2]  # tomorrow 13:00 is 27 hours away: only the 36 h one
    assert sent == [
        "☀️ Randonnée · Hilltown, demain 13:00–17:00 : 15 à 15 °C, pluie 10 %, vent 5 km/h. "
        "(alerte n°2)"
    ]
    clock.now += timedelta(hours=4)
    assert await alerts.check(sent) == [home.id]
    assert sent[1].startswith("☀️ Vélo · maison, demain 13:00–17:00")


async def test_quiet_hours_a_mute_and_a_failed_delivery(make, sky, clock, tmp_path):
    alerts, sent = make(), Sent()
    alert = alerts.add("wingfoil", lead_hours=48)
    for hour in range(10, 16):
        sky.wind_knots[(1, hour)] = 16

    async def nobody(_text):
        return None

    assert await alerts.check(nobody) == []  # no channel took it: not counted as said
    clock.now = clock.now.replace(hour=23)
    assert await alerts.check(sent) == []  # quiet hours
    clock.now = clock.now.replace(hour=12)
    alerts.turn_down(alert, forever=False)
    assert await alerts.check(sent) == []  # not again today
    clock.now += timedelta(days=1)
    for hour in range(10, 16):
        sky.wind_knots[(2, hour)] = 16
    assert await alerts.check(sent) == [alert.id]
    assert "today 12:00–16:00" in sent[0]  # the pause was for yesterday only

    alerts.turn_down(alert, forever=True)
    reloaded = make()
    assert reloaded.alerts[0].off and reloaded.alerts[0].sent_for == ["2026-10-15"]
    clock.now += timedelta(days=1)
    for hour in range(10, 16):
        sky.wind_knots[(3, hour)] = 16
    assert await reloaded.check(sent) == [] and len(sent) == 1
    assert oct((tmp_path / "alerts.json").stat().st_mode)[-3:] == "600"


async def test_the_measured_wind_is_looked_at_every_round_and_said_once_a_day(make, sky, clock):
    knots = {"now": 9}

    async def live(spot, now):
        reading = {
            "source": "OpenWindMap",
            "station": "Pioupiou #2",
            "observed_at": "08:57",
            "avg_knots": knots["now"],
            "gust_knots": knots["now"] + 5,
            "direction": "W",
            "direction_ok": True,
            "distance_km": 3.2,
        }
        return {"name": spot.name, "readings": [reading]}

    alerts, sent = make(live=live), Sent()
    alert = alerts.add("windsurf", spot="North Beach", when="now", gust_knots=20)
    assert await alerts.check(sent) == []
    knots["now"] = 14  # past the spot's 12 knots, but gusts of 19
    assert await alerts.check(sent) == []
    knots["now"] = 16
    assert await alerts.check(sent) == [alert.id]
    assert await alerts.check(sent) == []
    assert sent == [
        "🌬️ Windsurf · North Beach, now: 16 kn W, gusts 21 kn, measured at 08:57 by "
        "Pioupiou #2 (3.2 km away). (alert no. 1)"
    ]
    assert sky.calls == 0  # no forecast read for it
    clock.now += timedelta(days=1)
    assert await alerts.check(sent) == [alert.id]


async def test_what_can_never_fire_is_refused(make):
    alerts = make(home=None, surf=())
    with pytest.raises(ToolError, match="SURF_SPOTS"):
        alerts.add("surf")
    with pytest.raises(ToolError, match="Unknown spot"):
        alerts.add("wingfoil", spot="River")
    with pytest.raises(ToolError, match="LIVE_WIND_SOURCES"):
        alerts.add("wingfoil", when="now")
    with pytest.raises(ToolError, match="wind sports only"):
        alerts.add("cycling", when="now")
    with pytest.raises(ToolError, match="give a place"):
        alerts.add("cycling")
    with pytest.raises(ToolError, match="One activity"):
        alerts.add("surf and cycling")
    with pytest.raises(ToolError, match="One activity"):
        alerts.add("underwater chess")
    with pytest.raises(ToolError, match="lead_hours"):
        alerts.add("wingfoil", lead_hours=500)
    assert alerts.alerts == []


async def test_the_settings_alerts_are_created_once_and_stay_off_when_turned_off(make, tmp_path):
    alerts = make(surf=())
    alerts.lead_hours = 12
    alerts.seed(["wingfoil", "surf", "cycling"])  # no surf spot: that one is left out
    assert [(a.activity, a.lead_hours, a.from_settings) for a in alerts.alerts] == [
        ("wingfoil", 12, True),
        ("cycling", 12, True),
    ]
    asked = alerts.add("cycling", place="Hilltown", latitude=5.0, longitude=6.0)
    alerts.turn_down(alerts.alerts[0], forever=True)

    again = make(surf=())
    again.seed(["wingfoil"])  # cycling left the settings: its alert goes, the asked one stays
    assert [(a.id, a.activity, a.off) for a in again.alerts] == [
        (1, "wingfoil", True),
        (asked.id, "cycling", False),
    ]
    kept = json.loads((tmp_path / "alerts.json").read_text())
    assert [a["id"] for a in kept] == [1, asked.id]


async def test_setting_one_after_a_read_waits_but_turning_one_down_never_does(make, ctx):
    alerts = make()
    registry = ToolRegistry(build_activity_alert_tools(alerts, language="en"))
    await registry.run("set_activity_alert", {"activity": "wingfoil"}, ctx)
    ctx.tainted = True
    parked = await registry.run("set_activity_alert", {"activity": "surf"}, ctx)
    assert parked["status"] == "waiting_for_the_user" and len(alerts.alerts) == 1

    muted = await registry.run("mute_activity_alert", {"id": 1}, ctx)
    assert muted["now"]["state"] == "paused until after 2026-10-14"
    off = await registry.run("mute_activity_alert", {"id": 1, "forever": True}, ctx)
    assert off["now"]["state"] == "off"
    back = await registry.run("resume_activity_alert", {"id": 1}, ctx)
    assert back["status"] == "waiting_for_the_user" and alerts.alerts[0].off
    listed = await registry.run("list_activity_alerts", {}, ctx)
    assert listed["alerts"][0]["spot"] == "any spot in season"
    with pytest.raises(ToolError, match="No activity alert"):
        await registry.run("mute_activity_alert", {"id": 9}, ctx)


def test_no_tools_without_the_alerts():
    assert build_activity_alert_tools(None, language="en") == []
