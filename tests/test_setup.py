import importlib.util
import json
import stat
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup.py"
ROOT = SCRIPT.parent.parent
spec = importlib.util.spec_from_file_location("iha_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

# The assistant's name, no user name, language, time zone.
START = ["", "", "en", "UTC"]


def essentials(
    *,
    docker=True,
    identity=START,
    model=("", "claude-test"),  # Anthropic and its model
    home=("",),  # no city
    home_assistant=("n",),
    access=("",),  # this machine only; never asked without Docker
    channels=("siri",),
    features="",  # the one menu of what is optional: nothing
):
    """The answers of the short essential path, in the order the wizard asks them, down
    to the menu; a feature's own answers follow."""
    return [
        "" if docker else "n",
        *identity,
        *model,
        *home,
        *home_assistant,
        *(access if docker else ()),
        *channels,
        features,
    ]


def scripted(answers):
    queue = list(answers)

    def read(_prompt):
        return queue.pop(0)

    return read, queue


def read_env(path):
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if line and line[0] != "#"
    )


def run_with(tmp_path, answers, secrets_typed, **kwargs):
    read, queue = scripted(answers)
    read_secret, secret_queue = scripted(secrets_typed)
    printed = []
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / ".env"
    kwargs.setdefault("lookup", lambda *_: None)
    kwargs.setdefault("find_chat", lambda _token: None)
    assert setup.run(setup.Prompter(read, read_secret, printed.append), path, **kwargs)
    assert queue == [] and secret_queue == []
    return read_env(path), "\n".join(printed)


def test_the_essential_path_alone_asks_little_and_writes_a_working_env(tmp_path, monkeypatch):
    # Docker, Claude, no home, no Home Assistant, this machine only, no channel, nothing more.
    answers = essentials(channels=("",))
    assert len(answers) == 12  # a dozen questions, most answered with Enter
    env, output = run_with(tmp_path, answers, ["sk-secret"])
    assert set(env) == {
        "AGENT_API_TOKEN",
        "LLM_PROVIDER",
        "LLM_API_KEY",
        "LLM_MODEL",
        "ASSISTANT_NAME",
        "LANGUAGE",
        "TIMEZONE",
    }
    assert "Still empty" not in output and "docker compose up -d --build" in output
    assert "http://127.0.0.1:8000" in output
    # What can't be set up here is said, with the reason.
    assert "Not offered here: browser (needs the agent reached through" in output
    assert "Not offered here: jobs (needs a chat or a push" in output

    from intelligent_home_agent.config import Settings

    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=tmp_path / ".env")
    assert settings.llm_model == "claude-test" and settings.web_search_backend == "native"


def test_setup_writes_env_without_printing_secrets(tmp_path):
    answers = essentials(
        identity=["Nova", "Sam", "fr", "Europe/Paris"],
        home=("Springfield", "y", "y"),  # the city, look it up, use it
        channels=("siri,push", "https://ntfy.example.com/topic"),
        features="calendar, documents, briefing, outdoor",
    )
    # The calendar: provider, address, username, the agent may add events, to the server's
    # default calendar, contacts' birthdays.
    answers += ["nextcloud", "https://cloud.example.com/", "someone", "y", "", "y"]
    answers += ["letter", "gemini", "y"]  # paper, generated pictures, code sandbox
    # The briefing: calendar, weather, activities; then sent by itself: not a time (asked
    # again), 7:30, and no recap.
    answers += ["1,6,7", "detailed", "7h", "7:30", ""]
    answers += [
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
        "wingfoil, 2",  # alerts: the wind sport and cycling
        "100",  # how early: too far, asked again
        "36",
        "",  # quiet from 22
        "7",  # until 7
    ]
    lookups = []

    def lookup(city, language):
        lookups.append((city, language))
        return {"name": city, "country": "Nowhere", "country_code": "XX",
                "latitude": 0.0, "longitude": 0.0}  # fmt: skip

    # The model's key, no ntfy token, the calendar's password, the image model's key.
    typed = ["sk-very-secret", "", "caldav-pass", "gen-secret"]
    env, output = run_with(tmp_path, answers, typed, lookup=lookup)

    assert lookups == [("Springfield", "fr")]
    assert env["ASSISTANT_NAME"] == "Nova" and env["USER_NAME"] == "Sam"
    assert "MEMORY_STORE" not in env  # local files, the default
    assert env["LANGUAGE"] == "fr"
    assert env["HOME_LATITUDE"] == "0.000" and env["HOME_COUNTRY"] == "XX"
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
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    # Nothing is written that the file's groups don't know, and nothing internal.
    assert set(env) <= {k for _, keys in setup.GROUPS for k in keys}

    for secret in ("sk-very-secret", "gen-secret", "caldav-pass", env["AGENT_API_TOKEN"]):
        assert secret not in output
    assert "LLM_API_KEY = (set)" in output
    assert "COMPOSE_FILE" not in env and "http://127.0.0.1:8000" in output
    assert "WEB_PAGES_ENABLED" not in env and "INVESTIGATIONS_ENABLED" not in env
    assert "sent as a push" in output and "build_shortcuts.py" in output


def test_a_local_model_reached_over_tailscale(tmp_path):
    answers = essentials(
        model=("ollama", "y", "some-model"),  # run Ollama in Docker
        access=("funnel", "agent"),  # and the node's name
        channels=("scheduled",),
        features="devices, web, knowledge",
    )
    answers += ["hue", "http://192.168.1.50"]  # which devices, the bridge
    answers += ["searxng", "y", "", ""]  # in Docker; web pages, no investigations
    answers += ["someone/notes"]
    typed = ["", "tskey-secret", "hue-key", "kb-github-secret"]
    env, output = run_with(tmp_path, answers, typed)
    assert env["HUE_BRIDGE_URL"] == "http://192.168.1.50" and env["HUE_APP_KEY"] == "hue-key"
    assert "hue-key" not in output
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
        assert (ROOT / file).is_file()


def test_own_domain_with_another_server_and_missing_secrets(tmp_path):
    answers = essentials(
        model=("openai-compatible", "http://10.0.0.5:8080/v1", "m"),
        access=("caddy", "", "agent.example.com"),  # a domain is needed: asked again
        features="web, memory",
    )
    # No search, no web pages, investigations: with a domain, the agent knows its public
    # address. Then no memory.
    answers += ["off", "n", "y", "off"]
    env, output = run_with(tmp_path, answers, [""])
    assert env["WEB_PAGES_ENABLED"] == "false" and env["INVESTIGATIONS_ENABLED"] == "true"
    assert env["PUBLIC_URL"] == "https://agent.example.com"
    assert env["LLM_BASE_URL"] == "http://10.0.0.5:8080/v1"
    assert env["AGENT_DOMAIN"] == "agent.example.com"
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/caddy.yml"
    assert "Still empty" not in output and "https://agent.example.com" in output
    assert env["WEB_SEARCH_ENABLED"] == "false" and "WEB_SEARCH_BACKEND" not in env
    assert env["MEMORY_STORE"] == "off" and "USER_NAME" not in env
    # With a domain the remote browser is on offer.
    assert "Not offered here: browser" not in output

    answers = essentials(model=("mistral", "m"), access=("cloudflare",), features="web")
    answers += ["brave", "", ""]
    # Neither the model's key, the tunnel's token nor the search key is typed.
    env, output = run_with(tmp_path / "other", answers, ["", "", ""])
    assert "LLM_API_KEY, TUNNEL_TOKEN, WEB_SEARCH_API_KEY." in output


def test_home_assistant_on_a_tailnet_brings_the_sidecar(tmp_path):
    # Home Assistant, at its default address; this machine only; then: it is on Tailscale.
    answers = essentials(home_assistant=("y", ""), access=("", "y"))
    env, output = run_with(tmp_path, answers, ["sk-secret", "ha-secret", ""])
    assert env["HA_URL"] == "http://homeassistant.local:8123" and env["HA_TOKEN"] == "ha-secret"
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/tailscale.yml"
    assert "ha-secret" not in output and "add to .env before starting: TS_AUTHKEY." in output


def test_file_drives_get_a_sidecar_a_password_and_their_list(tmp_path, monkeypatch):
    # Two drives (one read-only) and scans read by the model.
    answers = essentials(features="drives")
    answers += ["drive", "", "", "y", "sftp", "Bad Name", "nas", "n", "n", "y"]
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


def test_without_docker_no_compose_question_and_the_extras_to_install(tmp_path):
    answers = essentials(docker=False, model=("lmstudio", "m"), features="documents,web,memory")
    answers += ["", ""]  # the usual paper, no generated pictures
    answers += ["searxng", "http://10.0.0.6:8080", "", ""]
    # Memory in a repository (asked twice: owner/repo).
    answers += ["github", "notes", "someone/config"]
    env, output = run_with(tmp_path, answers, ["", "gh-write-secret"])
    assert "COMPOSE_FILE" not in env and "LLM_BASE_URL" not in env and "EXTRAS" not in env
    # Not Anthropic: the code sandbox is not offered. No country: A4.
    assert env["PAPER_SIZE"] == "a4" and "CODE_EXECUTION_ENABLED" not in env
    assert env["WEB_SEARCH_URL"] == "http://10.0.0.6:8080"
    assert (env["MEMORY_STORE"], env["MEMORY_REPO"]) == ("github", "someone/config")
    assert env["GITHUB_TOKEN"] == "gh-write-secret" and "gh-write-secret" not in output
    assert "MEMORY_CRYPT_KEY" in output and "Still empty" not in output
    assert 'pip install ".[documents]" && uvicorn intelligent_home_agent.main:app' in output
    # What is a container of its own is not offered without Docker.
    for name in ("drives", "knowledge", "browser", "jobs"):
        assert f"Not offered here: {name} (needs Docker Compose)." in output


def test_home_devices_ask_what_the_wizard_can_take_and_say_the_rest(tmp_path):
    answers = essentials(docker=False, model=("lmstudio", "m"), features="devices, health")
    # Which devices; the speaker's address; the PetKit account and its country.
    answers += ["nuki, petkit, netatmo, android-tv, devialet", "http://192.168.1.51"]
    answers += ["pets@example.com", "GB"]
    env, output = run_with(tmp_path, answers, ["", "nuki-secret", "petkit-secret"])
    assert env["NUKI_API_TOKEN"] == "nuki-secret" and env["PETKIT_PASSWORD"] == "petkit-secret"
    assert (env["PETKIT_USERNAME"], env["PETKIT_REGION"]) == ("pets@example.com", "GB")
    assert env["DEVIALET_URL"] == "http://192.168.1.51"
    assert "nuki-secret" not in output and "petkit-secret" not in output
    for script in ("unlock_codes_setup", "netatmo_login", "android_tv_pair", "withings_login"):
        assert f"scripts/{script}.py" in output
        assert (SCRIPT.parent / f"{script}.py").is_file()
    assert "apple_devices" not in output
    # The devices' optional libraries are named in the install line.
    assert 'pip install ".[documents,petkit,android-tv]"' in output

    # With Docker they are built into the image.
    env = {}
    read, _ = scripted(["apple"])
    setup.ask_devices(setup.Prompter(read, lambda _: "", lambda _line: None), env)
    assert env == {"EXTRAS": "documents,apple"}


def test_apple_reminders_notes_and_find_my():
    printed = []
    read, queue = scripted(["sam@example.com", "", "n"])
    env = {"EXTRAS": "documents,subscription"}
    setup.ask_apple(setup.Prompter(read, lambda _: "", printed.append), env)
    assert queue == []
    assert env == {
        "EXTRAS": "documents,subscription,icloud",
        "ICLOUD_APPLE_ID": "sam@example.com",
        "ICLOUD_FIND_MY": "false",
    }
    output = "\n".join(printed)
    assert "python -m intelligent_home_agent.icloud_web" in output and "docs/apple.md" in output

    # No Apple ID given: nothing is set, and no step is promised.
    read, _ = scripted([""])
    env, printed[:] = {}, []
    setup.ask_apple(setup.Prompter(read, lambda _: "", printed.append), env)
    assert env == {} and "icloud_web" not in "\n".join(printed)


def test_servers_at_a_hosting_provider():
    printed = []
    read, queue = scripted(["hetzner"])
    env = {}
    setup.ask_hosting(setup.Prompter(read, lambda _: "api-secret", printed.append), env)
    assert queue == []
    assert env == {"HOSTING_PROVIDER": "hetzner", "HOSTING_API_TOKEN": "api-secret"}
    assert "HOSTING_API_TOKEN" in setup.SECRET_KEYS
    assert "HOSTING_PROVIDER=hetzner" in setup.render(env)
    assert "api-secret" not in "\n".join(printed) and "docs/connectors.md" in "\n".join(printed)

    # No token: a provider alone would keep the agent from starting, so nothing is set.
    read, _ = scripted([""])
    env = {}
    setup.ask_hosting(setup.Prompter(read, lambda _: "", printed.append), env)
    assert env == {}

    from intelligent_home_agent.tools.hosting import PROVIDERS

    assert set(setup.HOSTING) == set(PROVIDERS)


def test_the_remote_browser_gets_a_login_page_on_a_domain_and_the_tailnet_alone_otherwise():
    printed = []

    def ask(access, answers, env):
        read, queue = scripted(answers)
        setup.ask_browser(setup.Prompter(read, read, printed.append), env, access)
        assert queue == []
        return setup.compose_files(env)

    env = {"AGENT_DOMAIN": "agent.example.com", "EXTRAS": "documents,icloud"}
    setup.add_file(env, "traefik")
    files = ask("traefik", ["", "", "", "y", "mullvad"], env)
    assert files == [
        "compose/traefik.yml",
        "compose/browser.yml",
        "compose/browser-login.yml",
        "compose/browser-traefik.yml",
        "compose/vpn.yml",
    ]
    assert all((ROOT / f).is_file() for f in files)
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
    setup.finish_compose(env)
    assert set(env) <= {k for _, keys in setup.GROUPS for k in keys}
    assert env["COMPOSE_FILE"] == "docker-compose.yml:" + ":".join(files)

    env = {"TS_HOSTNAME": "home-agent"}
    assert ask("tailscale", ["tail1234.ts.net", ""], env) == ["compose/browser.yml"]
    env.pop("_COMPOSE")
    assert env == {
        "TS_HOSTNAME": "home-agent",
        "EXTRAS": "documents,browser",
        "BROWSER_GATE": "tailnet",
        "TS_SERVE": "serve-browser",
        "BROWSER_PUBLIC_URL": "https://home-agent.tail1234.ts.net:8443",
    }
    assert (ROOT / "compose/tailscale-serve-browser.json").is_file()


def test_the_menu_offers_what_can_be_set_up_here_and_says_why_not_the_rest():
    offer = setup.features_on_offer
    options, missing = offer({}, docker=True, access="local")
    assert list(options) == [f for f in setup.FEATURES if f not in {"browser", "jobs"}]
    assert set(missing) == {"browser", "jobs"}
    # A way in for the browser's page, a chat for the jobs' reports: everything is on offer.
    for access in setup.BROWSER_ACCESS:
        options, missing = offer({"TELEGRAM_CHAT_IDS": "1"}, docker=True, access=access)
        assert list(options) == list(setup.FEATURES) and missing == {}
    options, missing = offer({"NTFY_URL": "x"}, docker=False, access="local")
    assert set(missing) == {"drives", "knowledge", "browser", "jobs"}
    assert {"mail", "calendar", "devices", "outdoor", "apple"} <= set(options)

    # Nothing is picked by default, and a pick leads to that feature's questions only.
    printed = []
    read, queue = scripted([""])
    assert setup.ask_features(setup.Prompter(read, None, printed.append), {}, docker=True,
                              access="local") == []  # fmt: skip
    assert queue == []
    read, queue = scripted(["memory", "off"])
    env = {}
    setup.ask_features(setup.Prompter(read, None, printed.append), env, docker=True, access="lan")
    assert queue == [] and env == {"MEMORY_STORE": "off"}
    # Every feature of the menu has a page that docs/setup.md links.
    guide = (ROOT / "docs" / "setup.md").read_text()
    pages = {"devices": "home-devices", "calendar": "mail-calendar", "mail": "mail-calendar",
             "github": "tasks", "briefing": "tasks", "apple": "apple"}  # fmt: skip
    for feature in setup.FEATURES:
        assert f"({pages.get(feature, feature)}.md" in guide, feature


def test_wizard_offers_every_provider_and_every_compose_file():
    from intelligent_home_agent import llm

    assert set(setup.MODELS) == set(llm.PROVIDERS)
    assert setup.KEYLESS == {p for p in llm.PROVIDERS if not llm.needs_key(p)}
    shipped = {f.stem for f in (ROOT / "compose").glob("*.yml")}
    assert shipped == set(setup.COMPOSE_ORDER) and len(setup.COMPOSE_ORDER) == len(shipped)
    assert set(setup.COMPOSE_FILES.values()) <= shipped
    for access, gate in setup.BROWSER_ACCESS.items():
        assert access in setup.ACCESS
        assert (ROOT / f"compose/browser/gate-{gate}.Caddyfile").is_file()
    from intelligent_home_agent.tools.web import ENGINES

    assert set(setup.SEARCH) == {*ENGINES, "off"}
    from intelligent_home_agent.tools.documents import LETTER_COUNTRIES, PAPER_SIZES, paper_for

    assert set(setup.PAPERS) == set(PAPER_SIZES)
    assert {c.casefold() for c in setup.LETTER_COUNTRIES} == {
        c for c in LETTER_COUNTRIES if len(c) == 2
    }
    assert all(paper_for("auto", c) == "letter" for c in setup.LETTER_COUNTRIES)


def test_compose_files_keep_their_order_and_the_users_own():
    env = {}
    for name in ("coder", "vpn", "my/own.yml", "browser", "caddy", "browser"):
        setup.add_file(env, name)
    setup.finish_compose(env)
    assert env == {
        "COMPOSE_PATH_SEPARATOR": ":",
        "COMPOSE_FILE": "docker-compose.yml:compose/caddy.yml:compose/browser.yml"
        ":compose/vpn.yml:compose/coder.yml:my/own.yml",
    }
    env = {}
    setup.finish_compose(env)
    assert env == {}


def test_existing_env_is_kept_unless_told_otherwise(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KEEP=1\n")
    read, _ = scripted(["keep"])
    assert not setup.run(setup.Prompter(read, read, lambda _: None), path)
    assert path.read_text() == "KEEP=1\n"

    # Adding nothing changes nothing either.
    read, queue = scripted(["", "", ""])  # add, Docker, nothing picked
    assert not setup.run(setup.Prompter(read, read, lambda _: None), path)
    assert queue == [] and path.read_text() == "KEEP=1\n"

    # Replacing starts again from the first question.
    read, queue = scripted(["replace", *essentials()])
    read_secret, _ = scripted(["sk-secret"])
    assert setup.run(setup.Prompter(read, read_secret, lambda _: None), path)
    assert queue == [] and "KEEP" not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_features_are_added_to_an_existing_env_without_touching_the_rest(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    before = (
        "# My own notes stay.\n"
        "AGENT_API_TOKEN=a-long-token-typed-by-hand\n"
        "LLM_API_KEY=sk-by-hand\n"
        "HA_ENTITY_DENYLIST=light.bedroom\n"
        "COMPOSE_PATH_SEPARATOR=:\n"
        "COMPOSE_FILE=docker-compose.yml:compose/caddy.yml:my/own.yml\n"
        "AGENT_DOMAIN=agent.example.com\n"
        "NTFY_URL=https://ntfy.example.com/topic\n"
        "MAIL_ACCOUNTS=\n"
    )
    path.write_text(before)
    # Add; Docker; the features; a mailbox; the hosting provider; the browser's three names
    # and no VPN.
    answers = ["", "", "mail, hosting, browser"]
    answers += ["other", "Work", "mail.example.com", "sam", "n", "n"]
    answers += ["hetzner", "", "", "", "n"]
    read, queue = scripted(answers)
    read_secret, secret_queue = scripted(["mail-pass", "api-secret"])
    printed = []
    assert setup.run(setup.Prompter(read, read_secret, printed.append), path)
    assert queue == [] and secret_queue == []

    after = path.read_text()
    lines = after.splitlines()
    # What was there is still there, in its place; a setting that changes does so in place.
    for kept in before.splitlines()[:4] + before.splitlines()[6:8]:
        assert kept in lines
    assert lines[5] == (
        "COMPOSE_FILE=docker-compose.yml:compose/caddy.yml:compose/browser.yml"
        ":compose/browser-login.yml:compose/browser-caddy.yml:my/own.yml"
    )
    assert lines[8].startswith('MAIL_ACCOUNTS=[{"name": "Work"') and after.count("MAIL_ACC") == 1
    # What is new comes at the end.
    added = lines[lines.index("# Added by scripts/setup.py") + 1 :]
    assert "HOSTING_PROVIDER=hetzner" in added and "HOSTING_API_TOKEN=api-secret" in added
    assert "EXTRAS=documents,browser" in added
    assert "BROWSER_PUBLIC_URL=https://browser.example.com" in added
    assert not any(line.startswith(("_", "AGENT_API_TOKEN", "NTFY_URL")) for line in added)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    output = "\n".join(printed)
    assert "Updated" in output and "HOSTING_PROVIDER = hetzner" in output
    # Only what changed is listed, and no secret, old or new.
    assert "NTFY_URL" not in output and "AGENT_DOMAIN =" not in output
    for secret in ("mail-pass", "api-secret", "sk-by-hand", "a-long-token-typed-by-hand"):
        assert secret not in output
    assert "browser-auth.sh" in output and "Still empty" not in output

    from intelligent_home_agent.config import Settings

    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=path)
    assert settings.hosting_provider == "hetzner" and settings.mail_accounts[0].name == "Work"
    assert settings.ha_entity_denylist == ["light.bedroom"]


def test_an_existing_env_says_how_the_agent_is_reached():
    files = "docker-compose.yml:compose/tailscale.yml:compose/kb.yml"
    assert setup.access_of({"COMPOSE_FILE": files}) == "tailscale"
    assert setup.access_of({"COMPOSE_FILE": files, "TS_SERVE": "funnel"}) == "funnel"
    assert setup.access_of({"COMPOSE_FILE": "docker-compose.yml:compose/traefik.yml"}) == "traefik"
    assert setup.access_of({"COMPOSE_FILE": "x:compose/cloudflared.yml"}) == "cloudflare"
    assert setup.access_of({"AGENT_BIND": "0.0.0.0"}) == "lan"  # noqa: S104
    assert setup.access_of({}) == "local"
    for access in map(setup.access_of, ({}, {"AGENT_BIND": "x"}, {"COMPOSE_FILE": files})):
        assert access in setup.ACCESS


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
    assert setup.load_env(path) == env


def test_every_key_the_wizard_writes_is_a_setting_or_a_compose_variable():
    from intelligent_home_agent.config import Settings

    written = [k for _, keys in setup.GROUPS for k in keys]
    assert len(written) == len(set(written))
    example = (ROOT / ".env.example").read_text()
    for key in written:
        assert key.lower() in Settings.model_fields or f"{key}=" in example, key


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

    # The mail feature: one mailbox at least, then as many as wanted.
    read, queue = scripted([*answers[:4], "y", *answers[4:], ""])
    read_secret, _ = scripted(["app-pass", "other-pass"])
    env = {}
    setup.ask_mail(setup.Prompter(read, read_secret, lambda _: None), env)
    assert queue == [] and json.loads(env["MAIL_ACCOUNTS"]) == [first, second]


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
    quiet = lambda _line: None  # noqa: E731
    read, queue = scripted(["me/notes", "y", "y"])
    read_secret, _ = scripted(["ghp-secret"])
    env = {}
    setup.ask_github(setup.Prompter(read, read_secret, quiet), env)
    assert queue == []
    assert env == {
        "GITHUB_TOKEN": "ghp-secret",
        "NOTES_REPO": "me/notes",
        "NOTES_WRITE": "true",
        "GITHUB_REPOSITORIES": "true",
    }
    # No token: nothing else is asked.
    setup.ask_github(setup.Prompter(*scripted([])[:1], lambda _: "", quiet), unset := {})
    assert unset == {}

    # Without a chat or a push nothing is asked: a task would have no way to reach the
    # user, and the wizard says so.
    printed = []
    setup.ask_tasks(setup.Prompter(*scripted([])[:1], read_secret, printed.append), env)
    assert "need a chat or a push" in "\n".join(printed)
    env["NTFY_URL"] = "https://ntfy.example.com/topic"
    read, queue = scripted(["", "19:00", "y"])
    setup.ask_tasks(setup.Prompter(read, read_secret, quiet), env)
    assert queue == []
    assert env["EVENING_RECAP_AT"] == "19:00" and "MORNING_BRIEFING_AT" not in env
    assert env["TASKS_STORE"] == "github"


def test_claude_on_a_subscription_with_or_without_an_api_key(tmp_path):
    # Docker; the subscription with an API key beside it: where it answers, the model.
    answers = essentials(model=("claude-subscription", "chat, notification", ""))
    env, output = run_with(tmp_path, answers, ["oat-secret", "sk-secret"])
    assert (env["LLM_PROVIDER"], env["ENGINE"]) == ("anthropic", "subscription")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-secret" and env["LLM_API_KEY"] == "sk-secret"
    assert env["SUBSCRIPTION_DISPLAYS"] == "chat,notification"
    assert env["LLM_MODEL"] == "claude-sonnet-5-5"
    # The image is built with Claude Code in it.
    assert env["EXTRAS"] == "documents,subscription"
    assert "oat-secret" not in output and "sk-secret" not in output
    assert "claude setup-token" in output and "Still empty" not in output

    # Without Docker and without a key: no display to choose, and the extra to install.
    answers = essentials(docker=False, model=("claude-subscription", "claude-test"))
    env, output = run_with(tmp_path / "alone", answers, ["oat-secret", ""])
    assert env["ENGINE"] == "subscription" and "LLM_API_KEY" not in env
    assert "SUBSCRIPTION_DISPLAYS" not in env and "EXTRAS" not in env
    assert env["LLM_MODEL"] == "claude-test"
    assert "Still empty" not in output
    assert 'pip install ".[documents,subscription]"' in output

    # The token left for later is named among what is still missing; the API key is not.
    env, output = run_with(tmp_path / "later", answers, ["", ""])
    assert "Still empty, add to .env before starting: CLAUDE_CODE_OAUTH_TOKEN." in output

    # On the subscription alone, the code sandbox (the API's) is not offered.
    read, queue = scripted(["", ""])
    env = {"LLM_PROVIDER": "anthropic", "ENGINE": "subscription"}
    setup.ask_documents(setup.Prompter(read, lambda _: "", lambda _line: None), env)
    assert queue == [] and "CODE_EXECUTION_ENABLED" not in env


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


def chat_run(tmp_path, channel_answers, secrets_typed, *, docker=True, find_chat=None, more=()):
    """The wizard with a chat as the way to talk to it: its answers come where the
    channels are asked."""
    answers = essentials(
        docker=docker,
        model=("lmstudio", "m"),
        channels=channel_answers,
        features="briefing" if more else "",
    )
    typed = ["", *secrets_typed]  # no key for the local model
    return run_with(
        tmp_path, [*answers, *more], typed, find_chat=find_chat or (lambda _token: None)
    )


def test_whatsapp_in_ones_own_chat_brings_the_gateway_and_voice(tmp_path):
    # WhatsApp; no number of its own; the number, typed as people write it; voice messages.
    # Then the briefing: its sections, and no time of its own.
    env, output = chat_run(
        tmp_path,
        ["whatsapp", "n", "+1 555 0100", "y"],
        [],
        more=["weather", "short", "", ""],
    )
    assert env["WHATSAPP_CHAT_ID"] == "15550100@c.us" and "AGENT_WAHA_SESSION" not in env
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose/waha.yml"
    assert "WAHA_URL" not in env  # compose/waha.yml sets it
    assert (env["WHISPER_MODEL"], env["EXTRAS"]) == ("small", "documents,voice")
    for secret in (env["WAHA_API_KEY"], env["WAHA_DASHBOARD_PASSWORD"]):
        assert len(secret) >= 24 and secret not in output
    # A chat is somewhere the tasks can report: the briefing's time was asked.
    assert "sent in the chat" in output and "link the number in WAHA's dashboard" in output
    # And the coding jobs' reports too: they are on offer.
    assert "Not offered here: jobs" not in output


def test_a_number_of_its_own_and_a_gateway_run_elsewhere(tmp_path):
    answers = ["whatsapp", "y", "none", "15550100", "http://192.168.1.60:3000", "y"]
    env, output = chat_run(tmp_path, answers, [], docker=False)
    assert env["AGENT_WAHA_SESSION"] == "default" and env["WAHA_URL"] == "http://192.168.1.60:3000"
    assert "WAHA_DASHBOARD_PASSWORD" not in env and env["WHISPER_MODEL"] == "small"
    assert "Digits only" in output and "EXTRAS" not in env
    assert 'pip install ".[documents,voice]"' in output


def test_telegram_finds_the_chat_that_wrote_to_the_bot(tmp_path):
    asked = []

    def find_chat(token):
        asked.append(token)
        return 4242, "Sam"

    env, output = chat_run(
        tmp_path, ["telegram", "", "", "n"], ["123:bot-secret"], find_chat=find_chat
    )
    assert asked == ["123:bot-secret"]
    assert (env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_IDS"]) == ("123:bot-secret", "4242")
    assert "123:bot-secret" not in output and "COMPOSE_FILE" not in env
    assert "Telegram: write to your bot" in output and "WHISPER_MODEL" not in env


def test_telegram_without_a_chat_found_takes_the_id_typed_or_says_what_is_missing(tmp_path):
    # Both chats: WhatsApp's questions first, then Telegram's.
    env, _ = chat_run(
        tmp_path / "a",
        ["telegram, whatsapp", "n", "15550100", "", "", "77", "n"],
        ["123:s"],
        docker=False,
    )
    assert env["TELEGRAM_CHAT_IDS"] == "77" and env["WHATSAPP_CHAT_ID"] == "15550100@c.us"

    # Someone else wrote to the bot last: not confirmed, and no id typed.
    env, output = chat_run(
        tmp_path / "b",
        ["telegram", "", "n", "", "n"],
        ["123:s"],
        find_chat=lambda _t: (666, "X"),
    )
    assert "TELEGRAM_CHAT_IDS" not in env and "TELEGRAM_CHAT_IDS." in output

    env, output = chat_run(tmp_path / "c", ["telegram", "n"], [""])
    assert "TELEGRAM_BOT_TOKEN" not in env and "Skipped" in output


def test_coding_jobs_need_a_chat_or_a_push_for_their_reports(monkeypatch):
    quiet = lambda _line: None  # noqa: E731
    # Without a chat or a push nothing is asked: a job's report could not reach the user.
    env = {"USER_NAME": "Sam"}
    setup.ask_jobs(setup.Prompter(*scripted([])[:1], *scripted([])[:1], quiet), env)
    assert env == {"USER_NAME": "Sam"}

    env["NTFY_URL"] = "https://ntfy.example.com/topic"
    setup.add_file(env, "kb")
    # The commits' name (the user's, by default) and email; the review, on a repository
    # typed wrong then right.
    read, queue = scripted(["", "sam@example.com", "y", "my-agent", "sam/my-agent"])
    read_secret, secret_queue = scripted(["jobs-token", "claude-login"])
    setup.ask_jobs(setup.Prompter(read, read_secret, quiet), env)
    assert queue == [] and secret_queue == []
    assert env["JOBS_RUNNER"] == "claude_code" and len(env["CODER_TOKEN"]) > 30
    assert env["JOBS_GITHUB_TOKEN"] == "jobs-token"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-login"
    assert (env["JOBS_GIT_NAME"], env["JOBS_GIT_EMAIL"]) == ("Sam", "sam@example.com")
    setup.finish_compose(env)
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
