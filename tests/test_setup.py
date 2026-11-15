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
    "Sam",  # what it calls the user
    "fr",  # language
    "Europe/Paris",  # time zone
    "",  # provider: anthropic
    "claude-test",  # model
    "",  # open public web pages
    "",  # no investigations
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
    "openwindmap, 2",  # measured wind: the beacons and the airports
    "letter",  # paper
    "gemini",  # generated pictures
    "y",  # code sandbox
    "n",  # no Home Assistant
    "",  # no Hue
    "y",  # CalDAV
    "nextcloud",  # provider
    "https://cloud.example.com/",
    "someone",  # username
    "y",  # the agent may add events
    "",  # to the server's default calendar
    "y",  # contacts' birthdays
    "",  # no other devices
    "",  # memory: local files
    "",  # no knowledge base
    "",  # no file drives
    "",  # no chat
    "",  # access: this machine only
    "siri,push",  # channels
    "https://ntfy.example.com/topic",
    "7h",  # the briefing by itself: not a time, asked again
    "7:30",
    "",  # no recap by itself
    "wingfoil, 2",  # alerts: the wind sport and cycling
    "100",  # how early: too far, asked again
    "36",
    "",  # quiet from 22
    "7",  # until 7
    "",  # no coding jobs
]
SECRETS = ["sk-very-secret", "gen-secret", "caldav-pass", ""]


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
    assert env["ASSISTANT_NAME"] == "Nova" and env["USER_NAME"] == "Sam"
    assert "MEMORY_STORE" not in env  # local files, the default
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
    assert env["PAPER_SIZE"] == "letter" and env["CODE_EXECUTION_ENABLED"] == "true"
    assert env["GENERATION_GEMINI_API_KEY"] == "gen-secret"
    assert env["CALDAV_URL"] == env["CARDDAV_URL"] == "https://cloud.example.com/remote.php/dav"
    assert env["CALDAV_WRITE"] == "true" and "CALDAV_DEFAULT_CALENDAR" not in env
    assert env["LIVE_WIND_SOURCES"] == "openwindmap,metar"
    assert env["ACTIVITY_ALERTS"] == "wingfoil,cycling"
    assert env["ACTIVITY_ALERTS_LEAD_HOURS"] == "36"
    assert (env["ACTIVITY_ALERTS_QUIET_START"], env["ACTIVITY_ALERTS_QUIET_END"]) == ("22", "7")
    assert env["NTFY_URL"] == "https://ntfy.example.com/topic"
    assert env["MORNING_BRIEFING_AT"] == "07:30" and "EVENING_RECAP_AT" not in env
    assert "TASKS_STORE" not in env  # files on this machine, the default
    assert "HA_TOKEN" not in env and "NTFY_TOKEN" not in env
    assert len(env["AGENT_API_TOKEN"]) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    output = "\n".join(printed)
    for secret in ("sk-very-secret", "gen-secret", "caldav-pass", env["AGENT_API_TOKEN"]):
        assert secret not in output
    assert "LLM_API_KEY = (set)" in output
    assert "COMPOSE_FILE" not in env and "http://127.0.0.1:8000" in output
    assert "WEB_PAGES_ENABLED" not in env and "INVESTIGATIONS_ENABLED" not in env


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


# Name, no user name, language, time zone; then after the model and the search: web pages,
# no investigations, no city, no sections, no activities, the usual paper and no
# generated pictures.
START = ["", "", "en", "UTC"]
NOTHING_ELSE = ["", "", "", "weather", "short", "", "", ""]


def test_a_local_model_reached_over_tailscale(tmp_path):
    answers = ["", *START, "ollama", "y", "some-model", "searxng", "y", *NOTHING_ELSE]
    # No Home Assistant, Hue, no other devices, memory in local files, a knowledge base, no
    # file drives,
    # access, node name, channel.
    answers += ["n", "y", "http://192.168.1.20", "", "", "y", "someone/notes", ""]
    answers += ["", "funnel", "agent"]  # no chat, then the access
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
    # No web pages, investigations: with a domain, the agent knows its public address.
    # Then no Home Assistant, no Hue, no other devices, no memory.
    answers += ["n", "y", *NOTHING_ELSE[2:], "n", "", "", "off"]
    # No knowledge base, no file drives.
    answers += ["", "", "", "caddy", "", "agent.example.com", "", "siri"]
    env, output = run_with(tmp_path, answers, [""])
    assert env["WEB_PAGES_ENABLED"] == "false" and env["INVESTIGATIONS_ENABLED"] == "true"
    assert env["PUBLIC_URL"] == "https://agent.example.com"
    assert env["LLM_BASE_URL"] == "http://10.0.0.5:8080/v1"
    assert env["AGENT_DOMAIN"] == "agent.example.com"
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/caddy.yml"
    assert "Still empty" not in output and "https://agent.example.com" in output
    assert env["WEB_SEARCH_ENABLED"] == "false" and "WEB_SEARCH_BACKEND" not in env
    assert env["MEMORY_STORE"] == "off" and "USER_NAME" not in env
    answers = [
        "",
        *START,
        "mistral",
        "m",
        "brave",
        *NOTHING_ELSE,
        "n",
        "",
        "",  # no other devices
        "",  # memory: local files
        "",
        "",
        "",  # no chat
        "cloudflare",
        "siri",
    ]
    (tmp_path / "other").mkdir()
    env, output = run_with(tmp_path / "other", answers, ["", "", ""])
    assert "LLM_API_KEY, TUNNEL_TOKEN, WEB_SEARCH_API_KEY." in output


def test_file_drives_get_a_sidecar_a_password_and_their_list(tmp_path, monkeypatch):
    # Anthropic, nothing else; then two drives (one read-only) and scans read by the model.
    answers = ["", *START, "", "claude-test", *NOTHING_ELSE, "", "n", "", "", "", ""]
    answers += ["y", "drive", "", "", "y", "sftp", "Bad Name", "nas", "n", "n", "y"]
    answers += ["", "", "siri"]  # no chat, this machine only
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
    # No Home Assistant, no Hue, no other devices, memory in a repository (asked twice:
    # owner/repo), channel.
    answers += [*NOTHING_ELSE, "n", "", "", "github", "notes", "someone/config", "", "siri"]
    env, output = run_with(tmp_path, answers, ["", "gh-write-secret"])
    assert "COMPOSE_FILE" not in env and "LLM_BASE_URL" not in env
    # Not Anthropic: the code sandbox is not offered. No country: A4.
    assert env["PAPER_SIZE"] == "a4" and "CODE_EXECUTION_ENABLED" not in env
    assert (env["MEMORY_STORE"], env["MEMORY_REPO"]) == ("github", "someone/config")
    assert env["GITHUB_TOKEN"] == "gh-write-secret" and "gh-write-secret" not in output
    assert "MEMORY_CRYPT_KEY" in output and "Still empty" not in output
    assert "uvicorn intelligent_home_agent.main:app" in output


def test_other_devices_ask_what_the_wizard_can_take_and_say_the_rest(tmp_path):
    answers = ["n", *START, "lmstudio", "m", "off", *NOTHING_ELSE, "n", ""]
    # Other devices: yes; which; the PetKit account and its country.
    answers += ["y", "nuki, petkit, netatmo, android-tv, withings", "pets@example.com", "GB"]
    answers += ["", "", "siri"]  # memory in local files, no chat, channel
    env, output = run_with(tmp_path, answers, ["", "nuki-secret", "petkit-secret"])
    assert env["NUKI_API_TOKEN"] == "nuki-secret" and env["PETKIT_PASSWORD"] == "petkit-secret"
    assert (env["PETKIT_USERNAME"], env["PETKIT_REGION"]) == ("pets@example.com", "GB")
    assert "nuki-secret" not in output and "petkit-secret" not in output
    for script in ("unlock_codes_setup", "netatmo_login", "android_tv_pair", "withings_login"):
        assert f"scripts/{script}.py" in output
        assert (SCRIPT.parent / f"{script}.py").is_file()
    assert "apple_devices" not in output


def test_apple_reminders_notes_and_find_my_are_one_of_the_other_devices():
    printed = []
    read, queue = scripted(["y", "icloud", "sam@example.com", "", "n"])
    env = {"EXTRAS": "documents,subscription"}
    setup.ask_devices(setup.Prompter(read, lambda _: "", printed.append), env, docker=True)
    assert queue == []
    assert env == {
        "EXTRAS": "documents,subscription,icloud",
        "ICLOUD_APPLE_ID": "sam@example.com",
        "ICLOUD_FIND_MY": "false",
    }
    output = "\n".join(printed)
    assert "python -m intelligent_home_agent.icloud_web" in output and "docs/apple.md" in output

    # No Apple ID given: nothing is set, and no step is promised.
    read, _ = scripted(["y", "icloud", ""])
    env, printed[:] = {}, []
    setup.ask_devices(setup.Prompter(read, lambda _: "", printed.append), env, docker=False)
    assert env == {} and "icloud_web" not in "\n".join(printed)


def test_servers_at_a_hosting_provider_are_one_of_the_other_services():
    printed = []
    read, queue = scripted(["y", "hosting", "hetzner"])
    env = {}
    setup.ask_devices(setup.Prompter(read, lambda _: "api-secret", printed.append), env)
    assert queue == []
    assert env == {"HOSTING_PROVIDER": "hetzner", "HOSTING_API_TOKEN": "api-secret"}
    assert "HOSTING_API_TOKEN" in setup.SECRET_KEYS
    assert "HOSTING_PROVIDER=hetzner" in setup.render(env)
    assert "api-secret" not in "\n".join(printed) and "docs/connectors.md" in "\n".join(printed)

    # No token: a provider alone would keep the agent from starting, so nothing is set.
    read, _ = scripted(["y", "hosting", ""])
    env = {}
    setup.ask_devices(setup.Prompter(read, lambda _: "", printed.append), env)
    assert env == {}

    from intelligent_home_agent.tools.hosting import PROVIDERS

    assert set(setup.HOSTING) == set(PROVIDERS)


def test_the_remote_browser_gets_a_login_page_on_a_domain_and_the_tailnet_alone_otherwise():
    printed = []

    def ask(access, answers, env):
        read, queue = scripted(answers)
        files = setup.ask_browser(setup.Prompter(read, read, printed.append), env, access)
        assert queue == []
        return files

    env = {"AGENT_DOMAIN": "agent.example.com", "EXTRAS": "documents,icloud"}
    assert ask("traefik", [""], env) == [] and "BROWSER_DOMAIN" not in env

    files = ask("traefik", ["y", "", "", "", "y", "mullvad"], env)
    assert files == [
        "compose/browser.yml",
        "compose/browser-login.yml",
        "compose/browser-traefik.yml",
        "compose/vpn.yml",
    ]
    assert all((SCRIPT.parent.parent / f).is_file() for f in files)
    assert env["EXTRAS"] == "documents,icloud,browser"
    assert (env["BROWSER_DOMAIN"], env["AUTH_DOMAIN"]) == (
        "browser.example.com",
        "auth.example.com",
    )
    assert env["AUTH_COOKIE_DOMAIN"] == "example.com"
    assert env["BROWSER_PUBLIC_URL"] == "https://browser.example.com"
    secrets_made = [env[k] for k in ("AUTH_SESSION_SECRET", "AUTH_STORAGE_KEY", "AUTH_JWT_SECRET")]
    assert len(set(secrets_made)) == 3 and all(len(s) == 64 for s in secrets_made)
    assert env["VPN_SERVICE_PROVIDER"] == "mullvad" and len(env["VPN_CONTROL_KEY"]) == 64
    assert {"AUTH_SESSION_SECRET", "AUTH_STORAGE_KEY", "AUTH_JWT_SECRET", "VPN_CONTROL_KEY"} <= (
        setup.SECRET_KEYS
    )
    for secret in (*secrets_made, env["VPN_CONTROL_KEY"]):
        assert secret not in "\n".join(printed)
    written = {k for _, keys in setup.GROUPS for k in keys}
    assert set(env) <= written

    env = {"TS_HOSTNAME": "home-agent"}
    assert ask("tailscale", ["y", "tail1234.ts.net", ""], env) == ["compose/browser.yml"]
    assert env == {
        "TS_HOSTNAME": "home-agent",
        "EXTRAS": "documents,browser",
        "BROWSER_GATE": "tailnet",
        "TS_SERVE": "serve-browser",
        "BROWSER_PUBLIC_URL": "https://home-agent.tail1234.ts.net:8443",
    }
    assert (SCRIPT.parent.parent / "compose/tailscale-serve-browser.json").is_file()


def test_wizard_offers_every_provider_and_every_compose_file():
    from intelligent_home_agent import llm

    assert set(setup.MODELS) == set(llm.PROVIDERS)
    assert setup.KEYLESS == {p for p in llm.PROVIDERS if not llm.needs_key(p)}
    shipped = {f.stem for f in (SCRIPT.parent.parent / "compose").glob("*.yml")}
    browser = {"browser", "browser-login", "browser-caddy", "browser-traefik", "vpn"}
    assert shipped == {
        *setup.COMPOSE_FILES.values(),
        "ollama",
        "searxng",
        "kb",
        "drive",
        "waha",
        "coder",
        *browser,
    }
    for access, gate in setup.BROWSER_ACCESS.items():
        assert access in setup.ACCESS
        assert (SCRIPT.parent.parent / f"compose/browser/gate-{gate}.Caddyfile").is_file()
    from intelligent_home_agent.tools.web import ENGINES

    assert set(setup.SEARCH) == {*ENGINES, "off"}
    from intelligent_home_agent.tools.documents import LETTER_COUNTRIES, PAPER_SIZES, paper_for

    assert set(setup.PAPERS) == set(PAPER_SIZES)
    assert {c.casefold() for c in setup.LETTER_COUNTRIES} == {
        c for c in LETTER_COUNTRIES if len(c) == 2
    }
    assert all(paper_for("auto", c) == "letter" for c in setup.LETTER_COUNTRIES)


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


def test_a_mailbox_is_asked_for_by_provider_and_only_read_unless_agreed():
    from intelligent_home_agent.config import MailAccount

    answers = ["gmail", "", "sam@example.com", "n"]
    answers += ["other", "Work", "mail.example.com", "sam", "y"]
    read, queue = scripted(answers)
    read_secret, _ = scripted(["app-pass", "other-pass"])
    p = setup.Prompter(read, read_secret, lambda _: None)

    first, second = setup.ask_mailbox(p, 1), setup.ask_mailbox(p, 2)

    assert queue == []
    assert first == {
        "name": "Gmail",
        "host": "imap.gmail.com",
        "username": "sam@example.com",
        "password": "app-pass",
    }
    assert (second["name"], second["host"], second["write"]) == ("Work", "mail.example.com", True)
    assert not MailAccount(**first).write and MailAccount(**second).write


def test_a_calendar_on_another_server_asks_for_both_addresses():
    answers = ["other", "https://dav.example.com/", "sam", "n", "y", "https://dav.example.com/c/"]
    read, queue = scripted(answers)
    read_secret, _ = scripted(["dav-pass"])
    env: dict[str, str] = {}

    setup.ask_calendar(setup.Prompter(read, read_secret, lambda _: None), env)

    assert queue == [] and "CALDAV_WRITE" not in env
    assert env["CALDAV_URL"] == "https://dav.example.com/"
    assert env["CARDDAV_URL"] == "https://dav.example.com/c/"


def test_a_notes_repository_may_be_written_to_and_hold_the_tasks():
    answers = ["n", "n", "me/notes", "y", "y"]
    read, queue = scripted(answers)
    read_secret, _ = scripted(["ghp-secret"])
    env = {}
    prompter = setup.Prompter(read, read_secret, lambda _line: None)
    setup.ask_sources(prompter, env, ["reminders"])
    assert queue == []
    assert env == {
        "GITHUB_TOKEN": "ghp-secret",
        "NOTES_REPO": "me/notes",
        "NOTES_WRITE": "true",
        "GITHUB_REPOSITORIES": "true",
    }

    # Without a push nothing is asked: a task would have no way to reach the user.
    setup.ask_tasks(setup.Prompter(*scripted([])[:1], read_secret, lambda _line: None), env)
    env["NTFY_URL"] = "https://ntfy.example.com/topic"
    read, queue = scripted(["", "19:00", "y"])
    setup.ask_tasks(setup.Prompter(read, read_secret, lambda _line: None), env)
    assert queue == []
    assert env["EVENING_RECAP_AT"] == "19:00" and "MORNING_BRIEFING_AT" not in env
    assert env["TASKS_STORE"] == "github"


def test_claude_on_a_subscription_with_or_without_an_api_key(tmp_path):
    # Docker; the subscription with an API key beside it: where it answers, the model.
    answers = ["", *START, "claude-subscription", "chat, notification", ""]
    # A4, no pictures, no code sandbox; then nothing else, as in the tests above.
    # No Home Assistant, Hue, other devices; memory in local files; no knowledge base, no
    # drives; this machine only.
    answers += [*NOTHING_ELSE, "", "n", "", "", "", "", "", "", "", "siri"]
    env, output = run_with(tmp_path, answers, ["oat-secret", "sk-secret"])
    assert (env["LLM_PROVIDER"], env["ENGINE"]) == ("anthropic", "subscription")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-secret" and env["LLM_API_KEY"] == "sk-secret"
    assert env["SUBSCRIPTION_DISPLAYS"] == "chat,notification"
    assert env["LLM_MODEL"] == "claude-sonnet-5-5"
    # The image is built with Claude Code in it.
    assert env["EXTRAS"] == "documents,subscription"
    assert "oat-secret" not in output and "sk-secret" not in output
    assert "claude setup-token" in output and "Still empty" not in output
    # Claude searches the web itself: no search engine was asked for.
    assert "WEB_SEARCH_BACKEND" not in env

    # Without Docker and without a key: no display to choose, no sandbox to offer, and the
    # extra to install.
    (tmp_path / "alone").mkdir()
    answers = ["n", *START, "claude-subscription", "claude-test"]
    answers += [*NOTHING_ELSE, "n", "", "", "", "", "siri"]
    env, output = run_with(tmp_path / "alone", answers, ["oat-secret", ""])
    assert env["ENGINE"] == "subscription" and "LLM_API_KEY" not in env
    assert "SUBSCRIPTION_DISPLAYS" not in env and "EXTRAS" not in env
    assert env["LLM_MODEL"] == "claude-test" and "CODE_EXECUTION_ENABLED" not in env
    assert "Still empty" not in output
    assert 'pip install -e ".[subscription]"' in output

    # The token left for later is named among what is still missing; the API key is not.
    (tmp_path / "later").mkdir()
    env, output = run_with(tmp_path / "later", answers, ["", ""])
    assert "Still empty, add to .env before starting: CLAUDE_CODE_OAUTH_TOKEN." in output


def test_alerts_are_offered_only_with_a_push_and_for_what_can_be_judged():
    from intelligent_home_agent.config import LIVE_WIND_SOURCES

    assert tuple(setup.LIVE_SOURCES) == LIVE_WIND_SOURCES
    silent = setup.Prompter(*scripted([])[:1], None, lambda _line: None)
    # No push: nothing could carry an alert, so nothing is asked.
    env = {"OUTDOOR_ACTIVITIES": "surf,cycling", "HOME_LATITUDE": "0.0"}
    setup.ask_activity_alerts(silent, env)
    # A push, but surf without a beach and no home for the weather: nothing to offer.
    env = {"OUTDOOR_ACTIVITIES": "surf,cycling", "NTFY_URL": "https://ntfy.example.com/t"}
    setup.ask_activity_alerts(silent, env)
    assert "ACTIVITY_ALERTS" not in env

    env["HOME_LATITUDE"] = "0.0"
    printed = []
    read, queue = scripted(["surf", "1", "", "", ""])  # surf is not on offer: asked again
    setup.ask_activity_alerts(setup.Prompter(read, None, printed.append), env)
    assert queue == []
    assert env["ACTIVITY_ALERTS"] == "cycling" and env["ACTIVITY_ALERTS_LEAD_HOURS"] == "24"
    assert "  1. cycling: rain, wind speed, gusts and direction, temperature" in printed
    assert not any("surf:" in line for line in printed)

    # Declined: no setting at all.
    env = {"OUTDOOR_ACTIVITIES": "cycling", "NTFY_URL": "x", "HOME_LATITUDE": "0.0"}
    setup.ask_activity_alerts(setup.Prompter(*scripted([""])[:1], None, lambda _l: None), env)
    assert "ACTIVITY_ALERTS" not in env


def test_measured_wind_is_asked_with_a_spot_and_meteo_france_wants_its_id():
    spot = ["y", "South Bay", "0.0", "0.0", "w", "", "", "n"]
    read, queue = scripted(["surf", "y", *spot, "metar, meteofrance"])
    secret, _ = scripted(["mf-app-id"])
    env = {}
    setup.ask_activities(setup.Prompter(read, secret, lambda _line: None), env)
    assert queue == []
    assert env["LIVE_WIND_SOURCES"] == "metar,meteofrance"
    assert env["METEOFRANCE_APPLICATION_ID"] == "mf-app-id"
    assert "METEOFRANCE_APPLICATION_ID" in setup.SECRET_KEYS

    # No spot: the measured wind is not asked about.
    read, queue = scripted(["cycling", "y"])
    env = {}
    setup.ask_activities(setup.Prompter(read, secret, lambda _line: None), env)
    assert queue == [] and "LIVE_WIND_SOURCES" not in env


def chat_run(tmp_path, chat_answers, secrets_typed, *, docker=True, find_chat=None, tasks=True):
    """The wizard with nothing but a chat: its answers come where the chat is asked."""
    answers = ["" if docker else "n", *START, "lmstudio", "m", "off", *NOTHING_ELSE, "n", ""]
    # No other devices, memory in local files; with Docker no knowledge base nor drives.
    answers += ["", "", *(["", ""] if docker else [])]
    # The access with Docker, the channel, and with a chat set up the tasks' two times.
    answers += [*chat_answers, *([""] if docker else []), "siri", *(["", ""] if tasks else [])]
    # With Docker and a chat for their reports, the coding jobs are offered: no.
    answers += [""] if docker and tasks else []
    read, queue = scripted(answers)
    typed = ["", *secrets_typed]  # no key for the local model
    output: list[str] = []
    prompter = setup.Prompter(read=read, read_secret=lambda _p: typed.pop(0), write=output.append)
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / ".env"
    assert setup.run(
        prompter, path, lookup=lambda *_: None, find_chat=find_chat or (lambda _t: None)
    )
    assert queue == [] and typed == []
    env = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return env, "\n".join(output)


def test_whatsapp_in_ones_own_chat_brings_the_gateway_and_voice(tmp_path):
    # WhatsApp; no number of its own; the number, typed as people write it; voice messages.
    env, output = chat_run(tmp_path, ["whatsapp", "n", "+1 555 0100", "y"], [])
    assert env["WHATSAPP_CHAT_ID"] == "15550100@c.us" and "AGENT_WAHA_SESSION" not in env
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/waha.yml"
    assert "WAHA_URL" not in env  # compose/waha.yml sets it
    assert (env["WHISPER_MODEL"], env["EXTRAS"]) == ("small", "documents,voice")
    for secret in (env["WAHA_API_KEY"], env["WAHA_DASHBOARD_PASSWORD"]):
        assert len(secret) >= 24 and secret not in output
    # A chat is somewhere the tasks can report: the briefing's time was asked.
    assert "sent in the chat" in output and "link the number in WAHA's dashboard" in output


def test_a_number_of_its_own_and_a_gateway_run_elsewhere(tmp_path):
    answers = ["whatsapp", "y", "none", "15550100", "http://192.168.1.60:3000", "n"]
    env, output = chat_run(tmp_path, answers, [], docker=False)
    assert env["AGENT_WAHA_SESSION"] == "default" and env["WAHA_URL"] == "http://192.168.1.60:3000"
    assert "WAHA_DASHBOARD_PASSWORD" not in env and "WHISPER_MODEL" not in env
    assert "Digits only" in output


def test_telegram_finds_the_chat_that_wrote_to_the_bot(tmp_path):
    asked = []

    def find_chat(token):
        asked.append(token)
        return 4242, "Sam"

    env, output = chat_run(
        tmp_path, ["telegram", "", "", "n"], ["123:bot-secret"], find_chat=find_chat
    )
    assert asked == ["123:bot-secret"] and "Is this you: Sam (chat 4242)?" not in output
    assert (env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_IDS"]) == ("123:bot-secret", "4242")
    assert "123:bot-secret" not in output and "COMPOSE_FILE" not in env
    assert "Telegram: write to your bot" in output


def test_telegram_without_a_chat_found_takes_the_id_typed_or_says_what_is_missing(tmp_path):
    env, _ = chat_run(
        tmp_path / "a", ["both", "n", "15550100", "", "", "77", "n"], ["123:s"], docker=False
    )
    assert env["TELEGRAM_CHAT_IDS"] == "77" and env["WHATSAPP_CHAT_ID"] == "15550100@c.us"

    # Someone else wrote to the bot last: not confirmed, and no id typed.
    env, output = chat_run(
        tmp_path / "b",
        ["telegram", "", "n", "", "n"],
        ["123:s"],
        find_chat=lambda _t: (666, "X"),
        tasks=False,
    )
    assert "TELEGRAM_CHAT_IDS" not in env and "TELEGRAM_CHAT_IDS." in output

    env, output = chat_run(tmp_path / "c", ["telegram", "n"], [""], tasks=False)
    assert "TELEGRAM_BOT_TOKEN" not in env and "Skipped" in output


def test_coding_jobs_are_offered_with_a_chat_or_a_push_and_default_to_no(monkeypatch):
    quiet = lambda _line: None  # noqa: E731
    # Without a chat or a push nothing is asked: a job's report could not reach the user.
    env = {"USER_NAME": "Sam"}
    setup.ask_jobs(setup.Prompter(*scripted([])[:1], *scripted([])[:1], quiet), env)
    assert env == {"USER_NAME": "Sam"}

    env["NTFY_URL"] = "https://ntfy.example.com/topic"
    read, queue = scripted([""])
    setup.ask_jobs(setup.Prompter(read, *scripted([])[:1], quiet), env)
    assert queue == [] and "JOBS_RUNNER" not in env

    env["COMPOSE_FILE"] = "docker-compose.yml:compose/kb.yml"
    read, queue = scripted(["y", "", "sam@example.com", "y", "my-agent", "sam/my-agent"])
    read_secret, secret_queue = scripted(["jobs-token", "claude-login"])
    setup.ask_jobs(setup.Prompter(read, read_secret, quiet), env)
    assert queue == [] and secret_queue == []
    assert env["JOBS_RUNNER"] == "claude_code" and len(env["CODER_TOKEN"]) > 30
    assert env["JOBS_GITHUB_TOKEN"] == "jobs-token"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-login"
    assert (env["JOBS_GIT_NAME"], env["JOBS_GIT_EMAIL"]) == ("Sam", "sam@example.com")
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/kb.yml:compose/coder.yml"
    assert env["REVIEW_REPO"] == "sam/my-agent"
    assert {"CODER_TOKEN", "JOBS_GITHUB_TOKEN"} <= setup.SECRET_KEYS

    for key in ("NTFY_URL", "COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "USER_NAME"):
        env.pop(key)
    monkeypatch.setenv("AGENT_API_TOKEN", "test-token")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from intelligent_home_agent.config import Settings

    settings = Settings(_env_file=None)
    assert settings.review_repo == "sam/my-agent" and settings.jobs_runner == "claude_code"
    assert not settings.jobs_enabled  # compose/coder.yml sets CODER_URL
