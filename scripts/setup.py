#!/usr/bin/env python3
"""Interactive setup: asks a few questions and writes .env.

    python scripts/setup.py            # writes ./.env
    python scripts/setup.py --env path/to/.env

Standard library only, so it runs before anything is installed. Secrets (API keys,
passwords, tokens) are typed without echo and never printed back. The only network call
is the optional city lookup, which sends the city name to Open-Meteo's geocoding API.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from intelligent_home_agent.activities import (  # noqa: E402
    CATALOG,
    FORECASTS,
    forecasts_for,
    match_activities,
)

# Kept in step with intelligent_home_agent.llm (not imported: it needs httpx installed).
MODELS = {
    "anthropic": "Claude, with an Anthropic API key (the only one with web search)",
    "openai": "OpenAI, with an API key",
    "mistral": "Mistral, with an API key",
    "gemini": "Google Gemini, with an API key",
    "groq": "Groq, with an API key",
    "openrouter": "OpenRouter: many models behind one API key",
    "ollama": "a local model with Ollama: nothing leaves the machine, no key",
    "lmstudio": "a local model with LM Studio, no key",
    "openai-compatible": "any other server that speaks the OpenAI chat API (give its URL)",
}
KEYLESS = {"ollama", "lmstudio", "openai-compatible"}
SEARCH = {
    "duckduckgo": "DuckDuckGo: no account, best effort (it limits automated searches)",
    "searxng": "your own SearXNG search engine: no account, dependable",
    "brave": "Brave Search, with an API key",
    "tavily": "Tavily, with an API key",
    "off": "no web search",
}
ACCESS = {
    "local": "this machine only (127.0.0.1:8000): to try it, or behind your own proxy",
    "lan": "the home network, plain HTTP on port 8000: clients at home only",
    "tailscale": "your Tailscale network, HTTPS: your own devices, from anywhere",
    "funnel": "Tailscale Funnel: a public HTTPS address, no domain or open port needed",
    "cloudflare": "Cloudflare Tunnel: public HTTPS on a domain at Cloudflare, no open port",
    "caddy": "your own domain, HTTPS by Caddy: ports 80 and 443 open to this machine",
    "traefik": "your own domain, behind a Traefik you already run",
}
COMPOSE_FILES = {
    "tailscale": "tailscale",
    "funnel": "tailscale",
    "cloudflare": "cloudflared",
    "caddy": "caddy",
    "traefik": "traefik",
}

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
COMPASS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()
SECTIONS = {
    "calendar": "first appointment of the day (CalDAV calendar)",
    "travel": "when to leave for it (driving time from home)",
    "mail": "emails that need an answer (IMAP)",
    "pull_requests": "GitHub pull requests waiting for you",
    "reminders": "open reminders (from the phone, or left with a coding assistant)",
    "weather": "the weather",
    "activities": "outdoor sessions worth planning",
}
CHANNELS = {
    "siri": "Siri Shortcuts on iPhone, HomePod, Mac",
    "home_assistant": "Home Assistant Assist (voice satellites, phone app), and through it "
    "Google Home or Alexa routines",
    "push": "push notifications on phone and desktop (ntfy)",
    "scheduled": "a scheduler that calls the briefing endpoint (cron, Home Assistant, or a "
    "scheduled task in a chat or coding-assistant app)",
}
SECRET_KEYS = {
    "AGENT_API_TOKEN",
    "TS_AUTHKEY",
    "TUNNEL_TOKEN",
    "WEB_SEARCH_API_KEY",
    "SEARXNG_SECRET",
    "KB_TOKEN",
    "KB_GITHUB_TOKEN",
    "HUE_APP_KEY",
    "NUKI_API_TOKEN",
    "PETKIT_PASSWORD",
    "LLM_API_KEY",
    "HA_TOKEN",
    "CALDAV_PASSWORD",
    "MAIL_ACCOUNTS",
    "GITHUB_TOKEN",
    "NTFY_TOKEN",
}


class Prompter:
    """Console questions; tests pass their own ``read`` and ``read_secret``."""

    def __init__(
        self,
        read: Callable[[str], str] = input,
        read_secret: Callable[[str], str] = getpass.getpass,
        write: Callable[[str], None] = print,
    ) -> None:
        self._read = read
        self._read_secret = read_secret
        self.say = write

    def ask(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self._read(f"{question}{suffix}: ").strip()
        return answer or default

    def yes(self, question: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._read(f"{question} [{hint}]: ").strip().casefold()
            if not answer:
                return default
            if answer in {"y", "yes", "o", "oui"}:
                return True
            if answer in {"n", "no", "non"}:
                return False

    def secret(self, question: str) -> str:
        return self._read_secret(f"{question} (hidden, empty to skip): ").strip()

    def number(self, question: str, default: str = "") -> float:
        while True:
            try:
                return float(self.ask(question, default).replace(",", "."))
            except ValueError:
                self.say("  Please type a number.")

    def pick(self, question: str, options: dict[str, str], default: str) -> str:
        """One option, by number or name."""
        keys = list(options)
        for i, key in enumerate(keys, 1):
            self.say(f"  {i}. {key}: {options[key]}")
        while True:
            raw = self.ask(f"{question} (number or name)", default)
            key = keys[int(raw) - 1] if raw.isdigit() and 0 < int(raw) <= len(keys) else raw
            if key in options:
                return key
            self.say(f"  Unknown: {raw}")

    def choose(self, question: str, options: dict[str, str], default: list[str]) -> list[str]:
        keys = list(options)
        for i, key in enumerate(keys, 1):
            self.say(f"  {i}. {key}: {options[key]}")
        while True:
            raw = self.ask(f"{question} (numbers or names, comma-separated)", ",".join(default))
            picked, bad = [], []
            for item in filter(None, (p.strip() for p in raw.split(","))):
                key = keys[int(item) - 1] if item.isdigit() and 0 < int(item) <= len(keys) else item
                (picked if key in options else bad).append(key)
            if not bad:
                return list(dict.fromkeys(picked))
            self.say(f"  Unknown: {', '.join(bad)}")


def detect_timezone() -> str:
    if tz := os.environ.get("TZ"):
        return tz.lstrip(":")
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        pass
    try:
        return str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def geocode(city: str, language: str) -> dict | None:
    query = urllib.parse.urlencode({"name": city, "count": 1, "language": language})
    with urllib.request.urlopen(f"{GEOCODING_URL}?{query}", timeout=10) as resp:  # noqa: S310
        results = json.load(resp).get("results") or []
    return results[0] if results else None


def ask_identity(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Assistant")
    env["ASSISTANT_NAME"] = p.ask("Assistant name (what you call it)", "Assistant")
    while (language := p.ask("Language: en or fr", "en").casefold()) not in {"en", "fr"}:
        p.say("  en and fr are supported; see src/intelligent_home_agent/i18n.py to add one.")
    env["LANGUAGE"] = language
    while True:
        tz = p.ask("Time zone (IANA name)", detect_timezone())
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            p.say("  Unknown time zone, e.g. Europe/Paris, America/New_York, UTC.")
            continue
        env["TIMEZONE"] = tz
        break


def ask_home(p: Prompter, env: dict[str, str], lookup: Callable[[str, str], dict | None]) -> None:
    p.say("\n# Home location (weather, travel time, outdoor activities)")
    city = p.ask("Nearest town or city (empty to skip)")
    if not city:
        return
    env["HOME_CITY"] = city
    if p.yes("Look up its coordinates with Open-Meteo (sends the city name)?", True):
        try:
            found = lookup(city, env.get("LANGUAGE", "en"))
        except OSError as exc:
            p.say(f"  Lookup failed ({type(exc).__name__}); type the coordinates instead.")
            found = None
        if found:
            p.say(
                f"  Found {found['name']}, {found.get('country', '?')}: "
                f"{found['latitude']:.3f}, {found['longitude']:.3f}"
            )
            if p.yes("Use it?", True):
                env["HOME_LATITUDE"] = f"{found['latitude']:.3f}"
                env["HOME_LONGITUDE"] = f"{found['longitude']:.3f}"
                if code := found.get("country_code"):
                    env["HOME_COUNTRY"] = code
                return
    if p.yes("Type the coordinates yourself?", False):
        env["HOME_LATITUDE"] = str(p.number("Latitude"))
        env["HOME_LONGITUDE"] = str(p.number("Longitude"))


def ask_model(p: Prompter, env: dict[str, str], *, docker: bool) -> None:
    p.say("\n# Language model: who answers?")
    provider = p.pick("Provider", MODELS, "anthropic")
    env["LLM_PROVIDER"] = provider
    if provider == "openai-compatible":
        env["LLM_BASE_URL"] = p.ask("Server URL, usually ending in /v1")
    elif provider == "ollama" and docker:
        if p.yes("Run Ollama in Docker next to the agent (no: it already runs here)?", True):
            env["_COMPOSE_OLLAMA"] = "1"
        else:
            env["LLM_BASE_URL"] = "http://host.docker.internal:11434/v1"
    elif provider == "lmstudio" and docker:
        env["LLM_BASE_URL"] = "http://host.docker.internal:1234/v1"
    if key := p.secret("API key" + (" (if the server wants one)" if provider in KEYLESS else "")):
        env["LLM_API_KEY"] = key
    if provider == "anthropic":
        env["LLM_MODEL"] = p.ask("Model", "claude-sonnet-5-5")
        return
    p.say("  Pick a model that supports tool calling (function calling).")
    while not (model := p.ask("Model name, as the provider spells it")):
        p.say("  A model name is needed.")
    env["LLM_MODEL"] = model


def ask_search(p: Prompter, env: dict[str, str], *, docker: bool) -> None:
    if env["LLM_PROVIDER"] == "anthropic":
        return  # Claude searches the web with its own tool.
    p.say("\n# Web search: your model writes the answer, a search engine finds the pages")
    backend = p.pick("Search with", SEARCH, "duckduckgo")
    if backend == "off":
        env["WEB_SEARCH_ENABLED"] = "false"
        return
    env["WEB_SEARCH_BACKEND"] = backend
    if backend == "searxng":
        if docker and p.yes("Run SearXNG in Docker next to the agent?", True):
            env["_COMPOSE_SEARXNG"] = "1"
            env["SEARXNG_SECRET"] = secrets.token_hex(32)
        else:
            env["WEB_SEARCH_URL"] = p.ask("SearXNG address (JSON format enabled)")
    elif backend in {"brave", "tavily"}:
        if key := p.secret(f"{backend.capitalize()} API key"):
            env["WEB_SEARCH_API_KEY"] = key


def ask_access(p: Prompter, env: dict[str, str]) -> str | None:
    """How clients reach the agent; returns the address they will use, when known."""
    p.say("\n# Where will clients (phone, speaker, Home Assistant) reach the agent from?")
    access = p.pick("Access", ACCESS, "local")
    files = ["docker-compose.yml"]
    address = None
    if access == "local":
        address = "http://127.0.0.1:8000"
    elif access == "lan":
        env["AGENT_BIND"] = "0.0.0.0"  # noqa: S104
        address = "http://<this machine's address>:8000"
    elif access in {"tailscale", "funnel"}:
        if key := p.secret("Tailscale auth key (admin console > Settings > Keys)"):
            env["TS_AUTHKEY"] = key
        env["TS_HOSTNAME"] = p.ask("Name of the agent on your tailnet", "home-agent")
        if access == "funnel":
            env["TS_SERVE"] = "funnel"
        address = f"https://{env['TS_HOSTNAME']}.<your tailnet>.ts.net"
    elif access == "cloudflare":
        if token := p.secret("Tunnel token (Cloudflare Zero Trust > Networks > Tunnels)"):
            env["TUNNEL_TOKEN"] = token
        address = "https://<the public hostname you gave the tunnel>"
    else:
        while not (domain := p.ask("Domain name for the agent, e.g. agent.example.com")):
            p.say("  A domain name is needed.")
        env["AGENT_DOMAIN"] = domain
        address = f"https://{domain}"
        if access == "traefik":
            env["TRAEFIK_NETWORK"] = p.ask("Docker network Traefik shares with services", "proxy")
            env["TRAEFIK_CERTRESOLVER"] = p.ask("Traefik certificate resolver", "letsencrypt")
    if access in COMPOSE_FILES:
        files.append(f"compose/{COMPOSE_FILES[access]}.yml")
    elif env.get("HA_URL") and p.yes(
        "Is Home Assistant on your Tailscale network, out of this machine's direct reach?",
        False,
    ):
        if key := p.secret("Tailscale auth key (admin console > Settings > Keys)"):
            env["TS_AUTHKEY"] = key
        files.append("compose/tailscale.yml")
    if env.pop("_COMPOSE_OLLAMA", None):
        files.append("compose/ollama.yml")
    if env.pop("_COMPOSE_SEARXNG", None):
        files.append("compose/searxng.yml")
    if env.pop("_COMPOSE_KB", None):
        files.append("compose/kb.yml")
    if len(files) > 1:
        # ":" on every system, Windows included.
        env["COMPOSE_PATH_SEPARATOR"] = ":"
        env["COMPOSE_FILE"] = ":".join(files)
    return address


def ask_brief(p: Prompter, env: dict[str, str]) -> list[str]:
    p.say("\n# Morning briefing and evening recap: what should they cover?")
    sections = p.choose("Sections", SECTIONS, list(SECTIONS))
    env["BRIEF_SECTIONS"] = ",".join(sections)
    if "weather" in sections:
        detail = ""
        while detail not in {"short", "detailed"}:
            detail = p.ask("Weather detail: short (one sentence) or detailed", "short")
        env["WEATHER_DETAIL"] = detail
    else:
        env["WEATHER_DETAIL"] = "off"
    return sections


def _spot(p: Prompter, kind: str) -> dict:
    spot: dict = {"name": p.ask(f"  {kind} spot name")}
    spot["latitude"] = p.number("  Latitude")
    spot["longitude"] = p.number("  Longitude")
    if kind == "Wind":
        while True:
            raw = p.ask("  Good wind directions, e.g. W,SW,NW (empty: any)")
            directions = [d.strip().upper() for d in raw.split(",") if d.strip()]
            if all(d in COMPASS for d in directions):
                break
            p.say(f"  Use compass points: {' '.join(COMPASS)}")
        spot["directions"] = directions
        spot["min_knots"] = p.number("  Minimum wind in knots", "12")
    else:
        while (
            facing := p.ask("  Direction the beach faces (towards the sea), e.g. W").upper()
        ) not in COMPASS:
            p.say(f"  Use a compass point: {' '.join(COMPASS)}")
        spot["facing"] = facing
        spot["min_wave_m"] = p.number("  Smallest waves worth it, in meters", "0.5")
        spot["max_wave_m"] = p.number("  Biggest waves you want, in meters", "2.5")
    return spot


def ask_activities(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Outdoor activities")
    while True:
        text = p.ask(
            "What do you do outdoors? Free text, e.g. 'surf and cycling', 'I go out a lot' "
            "(empty for nothing)"
        )
        if not text:
            env["OUTDOOR_ACTIVITIES"] = ""
            return
        found, unknown = match_activities(text)
        if unknown and "outdoors" not in found:
            p.say(f"  Not recognized: {', '.join(unknown)}.")
            if p.yes("  Use the general outdoor weather (rain, temperature, UV) for those?", True):
                found.append("outdoors")
        if not found:
            p.say(f"  Known activities: {', '.join(CATALOG)}")
            continue
        p.say("  Forecasts proposed:")
        for key in found:
            p.say(f"  - {key}: {', '.join(FORECASTS[n] for n in CATALOG[key].needs)}")
        if p.yes("Use these?", True):
            break
    env["OUTDOOR_ACTIVITIES"] = ",".join(found)
    activities = [CATALOG[k] for k in found]
    needs = ", ".join(FORECASTS[n] for n in forecasts_for(activities))
    p.say(f"  The briefing will look at: {needs}.")
    for kind, key, label in (("wind", "WIND_SPOTS", "Wind"), ("surf", "SURF_SPOTS", "Surf")):
        if not any(a.kind == kind for a in activities):
            continue
        p.say(f"  {label} activities are judged per spot (you can add spots to .env later).")
        spots = []
        while p.yes(f"Add a {label.lower()} spot{' now' if not spots else ''}?", False):
            spots.append(_spot(p, label))
        if spots:
            env[key] = json.dumps(spots, ensure_ascii=False)


def ask_sources(p: Prompter, env: dict[str, str], sections: list[str]) -> None:
    p.say("\n# Data sources (each one is optional)")
    if p.yes("Control the home through Home Assistant?", True):
        env["HA_URL"] = p.ask("Home Assistant URL", "http://homeassistant.local:8123")
        if token := p.secret("Long-lived access token (HA profile > Security)"):
            env["HA_TOKEN"] = token
    if p.yes("Philips Hue lights, straight from the bridge (docs/home-devices.md)?", False):
        env["HUE_BRIDGE_URL"] = p.ask("Bridge address, e.g. http://192.168.1.20")
        if key := p.secret("App key (docs/home-devices.md shows how to get one)"):
            env["HUE_APP_KEY"] = key
    if "calendar" in sections and p.yes(
        "Read a CalDAV calendar (iCloud, Fastmail, Nextcloud...)?", True
    ):
        env["CALDAV_URL"] = p.ask("CalDAV URL, e.g. https://caldav.example.com/dav/")
        env["CALDAV_USERNAME"] = p.ask("Username")
        if password := p.secret("App-specific password"):
            env["CALDAV_PASSWORD"] = password
    if "mail" in sections:
        accounts = []
        while p.yes("Add a mailbox (IMAP, read-only)?", not accounts):
            account = {
                "name": p.ask("  Short name", f"Mail{len(accounts) + 1}"),
                "host": p.ask("  IMAP host, e.g. imap.gmail.com"),
                "username": p.ask("  Username"),
            }
            account["password"] = p.secret("  App password")
            accounts.append(account)
        if accounts:
            env["MAIL_ACCOUNTS"] = json.dumps(accounts)
    if "pull_requests" in sections or "reminders" in sections:
        if token := p.secret("GitHub token (classic, repo scope) for pull requests and reminders"):
            env["GITHUB_TOKEN"] = token
            if "reminders" in sections:
                repo = p.ask("Repo with the coding-assistant reminders, owner/repo (empty: none)")
                if repo:
                    env["ASSISTANT_REMINDERS_REPO"] = repo


# Devices and services set up beyond the wizard: what each needs once .env is written.
DEVICES = {
    "nuki": "a Nuki smart lock (a Nuki Web API token)",
    "petkit": "a PetKit feeder or litter box (a second PetKit account)",
    "netatmo": "Netatmo cameras, weather station or thermostat",
    "android-tv": "a Google TV / Android TV device: a TV, a projector, a box",
    "apple": "an Apple TV, HomePods",
    "withings": "a Withings scale (weigh-ins, read-only)",
}
DEVICE_STEPS = {
    "petkit": 'install the library: pip install ".[petkit]" (Docker: build with EXTRAS=petkit)',
    "netatmo": "run python scripts/netatmo_login.py and add the lines it writes to .env",
    "android-tv": "on the home network, run python scripts/android_tv_pair.py <address>",
    "apple": "on the home network, run python scripts/apple_devices.py profile <addresses>",
    "withings": "run python scripts/withings_login.py and add the lines it writes to .env",
}


def ask_devices(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# More of the home, without Home Assistant (docs/home-devices.md, docs/health.md)")
    if not p.yes("Set up a lock, cameras, a pet feeder, a TV, Apple devices or a scale?", False):
        return
    picked = p.choose("Which ones", DEVICES, [])
    if "nuki" in picked:
        if token := p.secret("Nuki Web API token (web.nuki.io > API)"):
            env["NUKI_API_TOKEN"] = token
        p.say("  Opening the door then waits for your yes each time; for a code or a secret")
        p.say("  phrase instead, run python scripts/unlock_codes_setup.py --totp --phrase.")
    if "petkit" in picked:
        env["PETKIT_USERNAME"] = p.ask("  PetKit account shared with the devices (email)")
        if password := p.secret("  Its password"):
            env["PETKIT_PASSWORD"] = password
        region = p.ask("  Country code of the PetKit account", env.get("HOME_COUNTRY", ""))
        if region:
            env["PETKIT_REGION"] = region
    steps = [(name, DEVICE_STEPS[name]) for name in picked if name in DEVICE_STEPS]
    if steps:
        p.say("  Once .env is written:")
        for name, step in steps:
            p.say(f"  - {name}: {step}.")


def ask_knowledge(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Knowledge base: your own notes, searched by the assistant (docs/knowledge.md)")
    p.say("  Markdown notes in a GitHub repository, e.g. the one claude-code-kit keeps.")
    if not p.yes("Use one?", False):
        return
    while "/" not in (repo := p.ask("Notes repository, owner/repo")):
        p.say("  Type it as owner/repo.")
    env["KB_REPO"] = repo
    if token := p.secret("GitHub token that can read it (fine-grained, Contents: read)"):
        env["KB_GITHUB_TOKEN"] = token
    env["KB_TOKEN"] = secrets.token_urlsafe(32)
    env["_COMPOSE_KB"] = "1"


def ask_channels(p: Prompter, env: dict[str, str]) -> list[str]:
    p.say("\n# How will you reach the assistant?")
    channels = p.choose("Channels", CHANNELS, ["siri"])
    if "push" in channels:
        suggested = f"https://ntfy.sh/{secrets.token_urlsafe(18)}"
        env["NTFY_URL"] = p.ask("ntfy topic URL (keep it long and random)", suggested)
        if token := p.secret("ntfy access token, for a protected server"):
            env["NTFY_TOKEN"] = token
    return channels


GROUPS = [
    ("API", ["AGENT_API_TOKEN"]),
    (
        "Docker Compose: how the agent is reached (docs/setup.md)",
        [
            "COMPOSE_PATH_SEPARATOR",
            "COMPOSE_FILE",
            "AGENT_BIND",
            "AGENT_DOMAIN",
            "TRAEFIK_NETWORK",
            "TRAEFIK_CERTRESOLVER",
            "TS_AUTHKEY",
            "TS_HOSTNAME",
            "TS_SERVE",
            "TUNNEL_TOKEN",
        ],
    ),
    ("Language model", ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]),
    (
        "Web search",
        [
            "WEB_SEARCH_ENABLED",
            "WEB_SEARCH_BACKEND",
            "WEB_SEARCH_URL",
            "WEB_SEARCH_API_KEY",
            "SEARXNG_SECRET",
        ],
    ),
    ("Assistant", ["ASSISTANT_NAME", "LANGUAGE", "TIMEZONE"]),
    ("Home", ["HOME_CITY", "HOME_COUNTRY", "HOME_LATITUDE", "HOME_LONGITUDE"]),
    ("Briefing", ["BRIEF_SECTIONS", "WEATHER_DETAIL"]),
    ("Outdoor activities", ["OUTDOOR_ACTIVITIES", "WIND_SPOTS", "SURF_SPOTS"]),
    ("Home Assistant", ["HA_URL", "HA_TOKEN"]),
    (
        "Home devices",
        [
            "HUE_BRIDGE_URL",
            "HUE_APP_KEY",
            "NUKI_API_TOKEN",
            "PETKIT_USERNAME",
            "PETKIT_PASSWORD",
            "PETKIT_REGION",
        ],
    ),
    ("Calendar", ["CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"]),
    ("Mail", ["MAIL_ACCOUNTS"]),
    ("GitHub", ["GITHUB_TOKEN", "ASSISTANT_REMINDERS_REPO"]),
    ("Knowledge base", ["KB_REPO", "KB_GITHUB_TOKEN", "KB_TOKEN"]),
    ("Push", ["NTFY_URL", "NTFY_TOKEN"]),
]


def render(env: dict[str, str]) -> str:
    lines = ["# Written by scripts/setup.py. Every setting is described in .env.example.", ""]
    for title, keys in GROUPS:
        present = [k for k in keys if k in env]
        if present:
            lines.append(f"# {title}")
            lines.extend(f"{k}={env[k]}" for k in present)
            lines.append("")
    return "\n".join(lines)


def summary(env: dict[str, str]) -> list[str]:
    return [
        f"  {k} = {'(set)' if k in SECRET_KEYS else env[k]}"
        for _, keys in GROUPS
        for k in keys
        if k in env and env[k]
    ]


NEXT_STEPS = {
    "siri": "Siri: python scripts/build_shortcuts.py on a Mac (docs/siri-shortcut.md).",
    "home_assistant": "Home Assistant, Google Home, Alexa: docs/integrations.md.",
    "push": "ntfy: install the app and subscribe to the topic URL in .env (NTFY_URL).",
    "scheduled": "Scheduled briefings: POST /v1/briefing (docs/integrations.md).",
}


def run(p: Prompter, path: Path, lookup: Callable[[str, str], dict | None] = geocode) -> bool:
    p.say("intelligent-home-agent setup. Press Enter to keep the value in brackets.")
    if path.exists() and not p.yes(f"{path} exists. Replace it?", False):
        p.say("Nothing written.")
        return False
    env: dict[str, str] = {"AGENT_API_TOKEN": secrets.token_urlsafe(32)}
    docker = p.yes("Will the agent run with Docker Compose (no: straight with Python)?", True)
    ask_identity(p, env)
    ask_model(p, env, docker=docker)
    ask_search(p, env, docker=docker)
    ask_home(p, env, lookup)
    sections = ask_brief(p, env)
    ask_activities(p, env)
    ask_sources(p, env, sections)
    ask_devices(p, env)
    if docker:
        ask_knowledge(p, env)
    address = ask_access(p, env) if docker else None
    env.pop("_COMPOSE_KB", None)
    env.pop("_COMPOSE_OLLAMA", None)
    env.pop("_COMPOSE_SEARXNG", None)
    channels = ask_channels(p, env)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render(env))
    p.say(f"\nWrote {path} (readable by you only):")
    for line in summary(env):
        p.say(line)
    missing = [
        key
        for key, needed in (
            ("LLM_API_KEY", env["LLM_PROVIDER"] not in KEYLESS),
            ("TS_AUTHKEY", "compose/tailscale.yml" in env.get("COMPOSE_FILE", "")),
            ("TUNNEL_TOKEN", "compose/cloudflared.yml" in env.get("COMPOSE_FILE", "")),
            ("WEB_SEARCH_API_KEY", env.get("WEB_SEARCH_BACKEND") in {"brave", "tavily"}),
            ("KB_GITHUB_TOKEN", "KB_REPO" in env),
        )
        if needed and key not in env
    ]
    if missing:
        p.say(f"\nStill empty, add to .env before starting: {', '.join(missing)}.")
    p.say("\nNext steps:")
    if docker:
        p.say("  Start: docker compose up -d --build")
        if "compose/ollama.yml" in env.get("COMPOSE_FILE", ""):
            p.say(f"  Then: docker compose exec ollama ollama pull {env['LLM_MODEL']}")
        p.say(f"  The agent will answer at {address}")
    else:
        p.say("  Start: pip install -e . && uvicorn intelligent_home_agent.main:app")
    p.say("  Check: docs/setup.md, step 6. Clients authenticate with AGENT_API_TOKEN from .env.")
    for channel in channels:
        p.say(f"  {NEXT_STEPS[channel]}")
    p.say("  Later: your bank, hosting provider or any service with an MCP server")
    p.say("  (docs/connectors.md), devices without Home Assistant (docs/home-devices.md).")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, default=Path(".env"), help="file to write")
    args = parser.parse_args()
    try:
        run(Prompter(), args.env)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled, nothing written.")


if __name__ == "__main__":
    main()
