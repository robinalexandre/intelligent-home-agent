import pytest

from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.departure import NOTE, Departure, record
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.proposals import build_proposal_tools


class Home:
    def __init__(self):
        self.lock = {"name": "Front door", "device": "smart_lock", "state": "locked"}
        self.lights = {"Living room": False, "Kitchen": False}
        self.playing = "paused"
        self.tv_on = False
        self.apple = []
        self.entities = {
            "lock": [{"entity_id": "lock.back", "name": "Back door", "state": "locked"}],
            "light": [{"entity_id": "light.hall", "name": "Hall", "state": "off"}],
            "media_player": [{"entity_id": "media_player.den", "name": "Den", "state": "idle"}],
        }
        self.calls = []
        self.fail = set()

    def tool(self, name, handler, **flags):
        async def run(args, ctx):
            self.calls.append((name, args))
            if name in self.fail:
                raise ToolError(f"{name} is down")
            return handler(args)

        return Tool(name, name, {"type": "object"}, run, **flags)

    def lock_door(self, args):
        self.lock["state"] = "locked"
        return {"sent": "lock", "confirmed": True}

    def set_lights(self, args):
        self.lights = dict.fromkeys(self.lights, False)
        return {"status": "done"}

    def direct(self):
        return [
            self.tool("get_lock_state", lambda a: {"locks": [dict(self.lock)]}),
            self.tool("lock_door", self.lock_door, needs_clean_turn=True),
            self.tool(
                "list_hue_lights",
                lambda a: {
                    "rooms": [
                        {"room": n, "lights": [{"name": n, "on": on, "reachable": True}]}
                        for n, on in self.lights.items()
                    ]
                },
            ),
            self.tool("set_hue_lights", self.set_lights, needs_clean_turn=True),
            self.tool("get_speaker_state", lambda a: {"playing": self.playing}),
            self.tool("control_speaker", lambda a: {"status": "done"}, needs_clean_turn=True),
            self.tool("get_tv_state", lambda a: {"on": self.tv_on, "app": "netflix"}),
            self.tool("control_tv", lambda a: {"status": "done"}, needs_clean_turn=True),
            self.tool("get_apple_media_state", lambda a: {"devices": list(self.apple)}),
            self.tool("control_apple_media", lambda a: {"status": "done"}, needs_clean_turn=True),
        ]

    def home_assistant(self):
        return [
            self.tool(
                "list_entities",
                lambda a: {"entities": [dict(e) for e in self.entities[a["domain"]]]},
            ),
            self.tool("call_service", lambda a: {"status": "done"}, needs_clean_turn=True),
        ]

    def registry(self, *, direct=True, ha=False):
        tools = [*(self.direct() if direct else []), *(self.home_assistant() if ha else [])]
        registry = ToolRegistry(tools)
        registry.extend(build_proposal_tools(registry))
        return registry


def waiting(conv):
    return [(p.tool, p.args, p.summary) for p in conv.proposals]


async def test_all_shut_says_nothing():
    home, conv = Home(), Conversation(id="c")
    report = await Departure(home.registry()).check(conv)
    assert report.text == "" and report.actions == []
    assert conv.proposals == [] and conv.messages == []


async def test_what_is_left_is_said_and_each_fix_waits_for_a_go():
    home, conv = Home(), Conversation(id="c")
    home.lock["state"] = "unlocked"
    home.lights["Kitchen"] = True
    home.playing = "playing"
    home.tv_on = True
    report = await Departure(home.registry(), tv_name="projector").check(conv)

    assert report.text.split("\n") == [
        "🏠 You left, but:",
        '🔓 "Front door" is not locked.',
        "💡 1 light(s) on: Kitchen.",
        "🔊 The speaker is still playing.",
        "📽️ The projector is still on (netflix).",
    ]
    assert waiting(conv) == [
        ("lock_door", {"lock": "Front door"}, 'Lock "Front door"'),
        ("set_hue_lights", {"target": "all", "on": False}, "Turn off the lights"),
        ("control_speaker", {"standby": True}, "Put the speaker in standby"),
        ("control_tv", {"power": "off"}, "Turn off the projector"),
    ]
    assert [a["number"] for a in report.actions] == [1, 2, 3, 4]
    # Nothing ran: only reads so far.
    assert not {name for name, _ in home.calls} & {"lock_door", "set_hue_lights", "control_tv"}


async def test_the_words_follow_the_language():
    home, conv = Home(), Conversation(id="c")
    home.lock["state"] = "unlocked"
    report = await Departure(home.registry(), language="fr").check(conv)
    assert report.text == "🏠 Tu es parti, mais :\n🔓 « Front door » n'est pas fermée à clé."
    assert conv.proposals[0].summary == "Fermer « Front door » à clé"


async def test_a_yes_in_a_later_turn_runs_exactly_the_fixes_shown():
    home, conv = Home(), Conversation(id="c")
    home.lock["state"] = "unlocked"
    home.lights["Kitchen"] = True
    registry = home.registry()
    report = await Departure(registry).check(conv)
    ctx = ToolContext(conv)

    # Not shown yet (main.with_waiting marks them): nothing can run.
    conv.turn += 1
    with pytest.raises(ToolError, match="Nothing is waiting"):
        await registry.run("run_proposals", {}, ctx)
    conv.mark_shown([a["number"] for a in report.actions])
    done = await registry.run("run_proposals", {"numbers": [2]}, ctx)

    assert [d["number"] for d in done["done"]] == [2]
    assert ("set_hue_lights", {"target": "all", "on": False}) in home.calls
    assert "lock_door" not in {name for name, _ in home.calls}
    assert [p.tool for p in conv.proposals] == ["lock_door"]


async def test_leaving_twice_does_not_park_the_same_fix_twice():
    home, conv = Home(), Conversation(id="c")
    home.lights["Kitchen"] = True
    check = Departure(home.registry())
    first = await check.check(conv)
    conv.mark_shown([a["number"] for a in first.actions])
    second = await check.check(conv)
    assert len(conv.proposals) == 1 and second.actions == []
    assert second.text == first.text


async def test_many_lights_are_named_up_to_a_few():
    home, conv = Home(), Conversation(id="c")
    home.lights = dict.fromkeys(["A", "B", "C", "D", "E", "F", "G"], True)
    report = await Departure(home.registry()).check(conv)
    assert "💡 7 light(s) on: A, B, C, D, E and 2 more." in report.text


async def test_the_apple_tv_and_a_playing_homepod_are_offered():
    home, conv = Home(), Conversation(id="c")
    home.apple = [
        {"device": "Living room TV", "kind": "apple_tv", "on": True, "app": "TV"},
        {"device": "Office", "kind": "homepod", "on": True, "playing": True},
        {"device": "Kitchen", "kind": "homepod", "on": True, "playing": False},
        {"device": "Bedroom", "kind": "homepod", "on": False, "playing": True},
        {"device": "Attic", "reachable": False, "note": "asleep"},
    ]
    report = await Departure(home.registry()).check(conv)
    assert report.text.split("\n")[1:] == [
        '📺 The Apple TV "Living room TV" is on (TV).',
        '🔈 The HomePod "Office" is still playing.',
    ]
    assert [(p.tool, p.args) for p in conv.proposals] == [
        ("control_apple_media", {"device": "Living room TV", "action": "turn_off"}),
        ("control_apple_media", {"device": "Office", "action": "pause"}),
    ]


async def test_an_open_door_is_said_but_never_locked():
    home, conv = Home(), Conversation(id="c")
    home.lock.update(state="unlocked", door="open")
    report = await Departure(home.registry()).check(conv)
    assert report.text.split("\n") == [
        "🏠 You left, but:",
        '🚪 The door of "Front door" was left open.',
        "I can't do anything about it from here.",
    ]
    assert conv.proposals == [] and report.actions == []


async def test_an_unreadable_lock_is_said():
    home, conv = Home(), Conversation(id="c")
    home.fail.add("get_lock_state")
    report = await Departure(home.registry()).check(conv)
    assert "⚠️ I couldn't read the lock." in report.text


async def test_an_unreadable_light_alone_stays_silent():
    home, conv = Home(), Conversation(id="c")
    home.fail.add("list_hue_lights")
    assert (await Departure(home.registry()).check(conv)).text == ""


async def test_an_opener_is_not_a_door_to_lock():
    home, conv = Home(), Conversation(id="c")
    home.lock.update(device="opener", state="online")
    assert (await Departure(home.registry()).check(conv)).text == ""


async def test_only_the_devices_set_up_are_read():
    home, conv = Home(), Conversation(id="c")
    home.lights["Kitchen"] = True
    tools = [t for t in home.direct() if "hue" in t.name]
    check = Departure(ToolRegistry(tools))
    report = await check.check(conv)
    assert check.watches_something
    assert report.text.split("\n")[1:] == ["💡 1 light(s) on: Kitchen."]
    assert {name for name, _ in home.calls} == {"list_hue_lights"}
    assert not Departure(ToolRegistry([])).watches_something


async def test_home_assistant_covers_what_no_direct_tool_does():
    home, conv = Home(), Conversation(id="c")
    home.entities["lock"][0]["state"] = "unlocked"
    home.entities["light"][0]["state"] = "on"
    home.entities["media_player"][0]["state"] = "playing"
    report = await Departure(home.registry(direct=False, ha=True)).check(conv)

    assert report.text.split("\n")[1:] == [
        '🔓 "Back door" is not locked: ask me to lock it.',
        "💡 1 light(s) on: Hall.",
        '🔊 "Den" is still playing.',
    ]
    # A lock is a sensitive domain in Home Assistant: said, never offered.
    assert waiting(conv) == [
        (
            "call_service",
            {"domain": "light", "service": "turn_off", "entity_id": ["light.hall"]},
            "Turn off the lights",
        ),
        (
            "call_service",
            {"domain": "media_player", "service": "media_pause", "entity_id": ["media_player.den"]},
            'Pause "Den"',
        ),
    ]


async def test_direct_devices_come_before_home_assistant_entities():
    home, conv = Home(), Conversation(id="c")
    home.entities["lock"][0]["state"] = "unlocked"
    home.entities["light"][0]["state"] = "on"
    home.entities["media_player"][0]["state"] = "playing"
    report = await Departure(home.registry(ha=True)).check(conv)
    assert report.text == ""
    assert "list_entities" not in {name for name, _ in home.calls}


def test_the_check_is_kept_in_the_history_for_the_yes():
    conv = Conversation(id="c")
    record(conv, "🏠 You left, but: ...")
    assert conv.messages == [
        {"role": "user", "content": NOTE},
        {"role": "assistant", "content": "🏠 You left, but: ..."},
    ]
