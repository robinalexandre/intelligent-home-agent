import importlib.util
import json
import stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup.py"
spec = importlib.util.spec_from_file_location("iha_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

ANSWERS = [
    "",  # with Docker Compose
    "Nova",  # name
    "fr",  # language
    "Europe/Paris",  # time zone
    "",  # provider: anthropic
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
    "",  # no Hue
    "y",  # CalDAV
    "https://caldav.example.com",
    "someone",  # username
    "",  # no knowledge base
    "",  # no file drives
    "",  # access: this machine only
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
    assert "COMPOSE_FILE" not in env and "http://127.0.0.1:8000" in output


def run_with(tmp_path, answers, secrets_typed):
    read, queue = scripted(answers)
    read_secret, secret_queue = scripted(secrets_typed)
    printed = []
    path = tmp_path / ".env"
    assert setup.run(setup.Prompter(read, read_secret, printed.append), path)
    assert queue == [] and secret_queue == []
    env = dict(
        line.split("=", 1) for line in path.read_text().splitlines() if line and line[0] != "#"
    )
    return env, "\n".join(printed)


# Name, language, time zone; then after the model: no city, no sections, no activities.
START = ["", "en", "UTC"]
NOTHING_ELSE = ["", "weather", "short", ""]


def test_a_local_model_reached_over_tailscale(tmp_path):
    answers = ["", *START, "ollama", "y", "some-model", "searxng", "y", *NOTHING_ELSE]
    # No Home Assistant, a knowledge base, access, node name, channel.
    answers += ["n", "y", "http://192.168.1.20", "y", "someone/notes", "", "funnel", "agent"]
    answers += ["scheduled"]
    env, output = run_with(tmp_path, answers, ["", "hue-key", "kb-github-secret", "tskey-secret"])
    assert env["HUE_BRIDGE_URL"] == "http://192.168.1.20" and "hue-key" not in output
    assert env["LLM_PROVIDER"] == "ollama" and "LLM_API_KEY" not in env
    assert "LLM_BASE_URL" not in env  # set by compose/ollama.yml
    assert env["COMPOSE_FILE"] == (
        "docker-compose.yml:compose/tailscale.yml:compose/ollama.yml:compose/searxng.yml"
        ":compose/kb.yml"
    )
    assert env["KB_REPO"] == "someone/notes" and env["KB_GITHUB_TOKEN"] == "kb-github-secret"
    assert env["KB_TOKEN"] not in output and "kb-github-secret" not in output
    assert env["WEB_SEARCH_BACKEND"] == "searxng" and len(env["SEARXNG_SECRET"]) == 64
    assert env["SEARXNG_SECRET"] not in output and "WEB_SEARCH_URL" not in env
    assert env["COMPOSE_PATH_SEPARATOR"] == ":"
    assert (env["TS_SERVE"], env["TS_HOSTNAME"]) == ("funnel", "agent")
    assert "tskey-secret" not in output and "Still empty" not in output
    assert "ollama pull some-model" in output and "https://agent.<your tailnet>.ts.net" in output
    for file in env["COMPOSE_FILE"].split(":"):
        assert (SCRIPT.parent.parent / file).is_file()


def test_own_domain_with_another_server_and_missing_secrets(tmp_path):
    answers = ["", *START, "openai-compatible", "http://10.0.0.5:8080/v1", "", "m", "off"]
    answers += [*NOTHING_ELSE, "n", "", "", "", "caddy", "", "agent.example.com", "siri"]
    env, output = run_with(tmp_path, answers, [""])
    assert env["LLM_BASE_URL"] == "http://10.0.0.5:8080/v1"
    assert env["AGENT_DOMAIN"] == "agent.example.com"
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/caddy.yml"
    assert "Still empty" not in output and "https://agent.example.com" in output
    assert env["WEB_SEARCH_ENABLED"] == "false" and "WEB_SEARCH_BACKEND" not in env
    answers = [
        "",
        *START,
        "mistral",
        "m",
        "brave",
        *NOTHING_ELSE,
        "n",
        "",
        "",
        "",
        "cloudflare",
        "siri",
    ]
    (tmp_path / "other").mkdir()
    env, output = run_with(tmp_path / "other", answers, ["", "", ""])
    assert "LLM_API_KEY, TUNNEL_TOKEN, WEB_SEARCH_API_KEY." in output


def test_file_drives_get_a_sidecar_a_password_and_their_list(tmp_path, monkeypatch):
    # Anthropic, nothing else; then two drives (one read-only) and scans read by the model.
    answers = ["", *START, "", "claude-test", *NOTHING_ELSE, "n", "", ""]
    answers += ["y", "drive", "", "", "y", "sftp", "Bad Name", "nas", "n", "n", "y"]
    answers += ["", "siri"]
    env, output = run_with(tmp_path, answers, ["sk-secret"])
    assert json.loads(env["DRIVES"]) == [
        {"name": "gdrive", "remote": "gdrive:", "backend": "drive"},
        {"name": "nas", "remote": "nas:", "backend": "sftp", "read_only": True},
    ]
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/drive.yml"
    assert len(env["DRIVE_PASSWORD"]) == 48 and env["DRIVE_PASSWORD"] not in output
    assert env["DRIVE_OCR"] == "model" and "docs/drives.md" in output

    from intelligent_home_agent.config import Settings

    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=tmp_path / ".env")
    assert [d.name for d in settings.drives] == ["gdrive", "nas"] and settings.drives[1].read_only
    assert not settings.drives_enabled  # compose/drive.yml sets DRIVE_URL


def test_without_docker_no_compose_question(tmp_path):
    answers = ["n", *START, "lmstudio", "m", "searxng", "http://10.0.0.6:8080"]
    answers += [*NOTHING_ELSE, "n", "", "siri"]
    env, output = run_with(tmp_path, answers, [""])
    assert "COMPOSE_FILE" not in env and "LLM_BASE_URL" not in env
    assert "uvicorn intelligent_home_agent.main:app" in output


def test_wizard_offers_every_provider_and_every_compose_file():
    from intelligent_home_agent import llm

    assert set(setup.MODELS) == set(llm.PROVIDERS)
    assert setup.KEYLESS == {p for p in llm.PROVIDERS if not llm.needs_key(p)}
    shipped = {f.stem for f in (SCRIPT.parent.parent / "compose").glob("*.yml")}
    assert shipped == {*setup.COMPOSE_FILES.values(), "ollama", "searxng", "kb", "drive"}
    from intelligent_home_agent.tools.web import ENGINES

    assert set(setup.SEARCH) == {*ENGINES, "off"}


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
