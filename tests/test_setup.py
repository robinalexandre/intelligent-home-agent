import importlib.util
import json
import stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup.py"
spec = importlib.util.spec_from_file_location("iha_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

ANSWERS = [
    "Nova",  # name
    "fr",  # language
    "Europe/Paris",  # time zone
    "claude-test",  # model
    "Springfield",  # city
    "y",  # look it up
    "y",  # use it
    "1,6,7",  # sections: calendar, weather, activities
    "detailed",  # weather detail
    "wing, bike and juggling",  # activities
    "y",  # general outdoor weather for juggling
    "y",  # use these
    "y",  # add a wind spot
    "North Beach",
    "0.0",
    "0.0",
    "w, sw",
    "",  # min knots: default
    "n",  # no more spots
    "n",  # no Home Assistant
    "y",  # CalDAV
    "https://caldav.example.com",
    "someone",  # username
    "siri,push",  # channels
    "https://ntfy.example.com/topic",
]
SECRETS = ["sk-very-secret", "caldav-pass", ""]


def scripted(answers):
    queue = list(answers)

    def read(_prompt):
        return queue.pop(0)

    return read, queue


def test_setup_writes_env_without_printing_secrets(tmp_path):
    read, queue = scripted(ANSWERS)
    read_secret, secret_queue = scripted(SECRETS)
    printed = []
    prompter = setup.Prompter(read, read_secret, printed.append)
    lookups = []

    def lookup(city, language):
        lookups.append((city, language))
        return {"name": city, "country": "Nowhere", "country_code": "XX",
                "latitude": 0.0, "longitude": 0.0}  # fmt: skip

    path = tmp_path / ".env"
    assert setup.run(prompter, path, lookup)

    assert queue == [] and secret_queue == []
    assert lookups == [("Springfield", "fr")]
    env = dict(
        line.split("=", 1) for line in path.read_text().splitlines() if line and line[0] != "#"
    )
    assert env["ASSISTANT_NAME"] == "Nova"
    assert env["LANGUAGE"] == "fr"
    assert env["HOME_LATITUDE"] == "0.000"
    assert env["BRIEF_SECTIONS"] == "calendar,weather,activities"
    assert env["WEATHER_DETAIL"] == "detailed"
    assert env["OUTDOOR_ACTIVITIES"] == "wingfoil,cycling,outdoors"
    assert json.loads(env["WIND_SPOTS"]) == [
        {"name": "North Beach", "latitude": 0.0, "longitude": 0.0,
         "directions": ["W", "SW"], "min_knots": 12.0}
    ]  # fmt: skip
    assert env["CALDAV_PASSWORD"] == "caldav-pass"
    assert env["NTFY_URL"] == "https://ntfy.example.com/topic"
    assert "HA_TOKEN" not in env and "NTFY_TOKEN" not in env
    assert len(env["AGENT_API_TOKEN"]) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    output = "\n".join(printed)
    for secret in ("sk-very-secret", "caldav-pass", env["AGENT_API_TOKEN"]):
        assert secret not in output
    assert "LLM_API_KEY = (set)" in output


def test_existing_env_is_kept_unless_confirmed(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KEEP=1\n")
    read, _ = scripted(["n"])
    assert not setup.run(setup.Prompter(read, read, lambda _: None), path)
    assert path.read_text() == "KEEP=1\n"


def test_written_env_loads_into_settings(tmp_path, monkeypatch):
    from intelligent_home_agent.config import Settings

    env = {"AGENT_API_TOKEN": "x", "LLM_API_KEY": "y", "OUTDOOR_ACTIVITIES": "surf,outdoors",
           "BRIEF_SECTIONS": "calendar,weather", "WEATHER_DETAIL": "short",
           "SURF_SPOTS": json.dumps([{"name": "A", "latitude": 0.0, "longitude": 0.0,
                                      "facing": "W"}])}  # fmt: skip
    path = tmp_path / ".env"
    path.write_text(setup.render(env))
    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=path)
    assert settings.outdoor_activities == ["surf", "outdoors"]
    assert settings.surf_spots[0].facing == "W"
