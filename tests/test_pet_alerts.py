import asyncio
from datetime import UTC, datetime

import pytest

from intelligent_home_agent import pet_alerts
from intelligent_home_agent.pet_alerts import PetAlerts

NOON = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 14, 23, 30, tzinfo=UTC)


class World:
    def __init__(self):
        self.status = {
            "litter_boxes": [{"name": "Litter", "litter_percent": 95, "waste_bin_full": False}],
            "feeders": [{"name": "Food", "food_low": False}],
        }
        self.now = NOON
        self.sent = []

    async def read(self):
        return self.status

    async def send(self, text):
        self.sent.append(text)


def alerts(world, tmp_path, **kwargs):
    return PetAlerts(world.read, tmp_path / "pets.json", "UTC", clock=lambda: world.now, **kwargs)


async def test_nothing_to_say_says_nothing(tmp_path):
    world = World()
    assert await alerts(world, tmp_path).check(world.send) == []
    assert world.sent == []


async def test_low_litter_is_said_once_until_it_goes_back_up(tmp_path):
    world = World()
    watch = alerts(world, tmp_path, litter_percent=50)
    world.status["litter_boxes"][0]["litter_percent"] = 40
    await watch.check(world.send)
    await watch.check(world.send)
    assert world.sent == ["🐱 Litter: 40% of litter left, time to add some."]
    world.status["litter_boxes"][0]["litter_percent"] = 52  # not back up enough
    await watch.check(world.send)
    world.status["litter_boxes"][0]["litter_percent"] = 45
    await watch.check(world.send)
    assert len(world.sent) == 1
    world.status["litter_boxes"][0]["litter_percent"] = 90
    await watch.check(world.send)
    world.status["litter_boxes"][0]["litter_percent"] = 30
    await watch.check(world.send)
    assert len(world.sent) == 2


async def test_food_uses_the_level_or_the_feeders_own_flag(tmp_path):
    world = World()
    watch = alerts(world, tmp_path, food_percent=45)
    world.status["feeders"][0]["food_low"] = True
    await watch.check(world.send)
    assert "the food is low" in world.sent[0]
    world.status["feeders"].append({"name": "Bowl 2", "food_left_percent": 30})
    await watch.check(world.send)
    assert world.sent[1] == "🍽️ Bowl 2: 30% of food left, time to refill it."


async def test_the_words_follow_the_language(tmp_path):
    world = World()
    world.status["litter_boxes"][0]["litter_percent"] = 40
    await alerts(world, tmp_path, language="fr").check(world.send)
    assert world.sent == ["🐱 Litter : plus que 40 % de litière, pense à en remettre."]


async def test_a_full_bin_is_reminded_until_the_box_says_empty(tmp_path):
    world = World()
    watch = alerts(world, tmp_path, remind_minutes=120)
    world.status["litter_boxes"][0]["waste_bin_full"] = True
    await watch.check(world.send)
    assert world.sent == ["🗑️ Litter: the waste bin is full, it needs emptying."]
    world.now = NOON.replace(minute=30)
    await watch.check(world.send)
    assert len(world.sent) == 1
    world.now = NOON.replace(hour=14, minute=1)
    await watch.check(world.send)
    assert world.sent[1].endswith("(reminder).")
    world.status["litter_boxes"][0]["waste_bin_full"] = False
    world.now = NOON.replace(hour=17)
    await watch.check(world.send)
    assert len(world.sent) == 2
    # Full again later: said at once, as a first time.
    world.status["litter_boxes"][0]["waste_bin_full"] = True
    await watch.check(world.send)
    assert len(world.sent) == 3 and "(reminder)" not in world.sent[2]


async def test_quiet_hours_hold_everything_until_morning(tmp_path):
    world = World()
    world.now = NIGHT
    watch = alerts(world, tmp_path)
    world.status["litter_boxes"][0].update(litter_percent=10, waste_bin_full=True)
    await watch.check(world.send)
    assert world.sent == []
    world.now = NOON
    await watch.check(world.send)
    assert len(world.sent) == 2


async def test_the_watch_reads_nothing_at_night(tmp_path, monkeypatch):
    world = World()
    world.now = NIGHT
    reads = []

    async def read():
        reads.append(world.now)
        return world.status

    watch = PetAlerts(read, tmp_path / "p.json", "UTC", clock=lambda: world.now)
    naps = []

    async def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            world.now = NOON
        if len(naps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(pet_alerts.asyncio, "sleep", nap)
    with pytest.raises(asyncio.CancelledError):
        await watch.watch(world.send)
    assert reads == [NOON]
    assert naps == [pet_alerts.QUIET_POLL_SECONDS] * 2 + [180 * 60]


async def test_the_state_survives_a_restart(tmp_path):
    world = World()
    world.status["litter_boxes"][0]["litter_percent"] = 10
    await alerts(world, tmp_path).check(world.send)
    await alerts(world, tmp_path).check(world.send)
    assert len(world.sent) == 1
