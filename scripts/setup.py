#!/usr/bin/env python3
"""Interactive setup: asks a few questions and writes .env.

    python scripts/setup.py            # writes ./.env, or adds features to the one there
    python scripts/setup.py --env path/to/.env

A short essential path first (a name, a model, where you live, Home Assistant, how the
agent is reached, how you talk to it), then one menu of optional features, none picked by
default. Run on an existing .env, it offers to add features to it: only what you pick is
asked, and every other line of the file is kept as it is.

Standard library only, so it runs before anything is installed. Secrets (API keys,
passwords, tokens) are typed without echo and never printed back. The only network calls
are optional: the city lookup, which sends the city name to Open-Meteo's geocoding API,
and reading your chat's id from Telegram with the bot token you typed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
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
    "anthropic": "Claude, with an Anthropic API key",
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
# Not a provider of its own: Claude (anthropic), answered by Claude Code on a Claude plan
# instead of the API (ENGINE=subscription, docs/subscription.md).
SUBSCRIPTION = "claude-subscription"
SUBSCRIPTION_HELP = (
    "Claude, on your Claude subscription through Claude Code: no API bill, slower replies"
)
DISPLAYS = {
    "chat": "replies read in a chat: the wait is fine",
    "notification": "briefings and other notifications: a few seconds more",
    "voice": "spoken replies: a few seconds more of silence before each answer",
}
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
# Every file of compose/, in the order COMPOSE_FILE lists them: a file that changes
# another's service comes after it (the browser's Caddy after Caddy, the VPN after the
# browser).
COMPOSE_ORDER = [
    "caddy",
    "traefik",
    "tailscale",
    "cloudflared",
    "browser",
    "browser-login",
    "browser-caddy",
    "browser-traefik",
    "vpn",
    "ollama",
    "searxng",
    "kb",
    "drive",
    "waha",
    "coder",
]

# A short name suggested for a drive, by rclone type, when the type itself reads badly.
DRIVE_NAMES = {"drive": "gdrive", "iclouddrive": "icloud", "google photos": "photos"}
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
COMPASS = "N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split()
SECTIONS = {
    "calendar": "first appointment of the day (CalDAV calendar: iCloud, Fastmail, Nextcloud...)",
    "travel": "when to leave for it (driving time from home)",
    "mail": "emails that need an answer (IMAP: Gmail, iCloud, Fastmail, your host...)",
    "pull_requests": "GitHub pull requests waiting for you",
    "reminders": "open reminders (from the phone, or left with a coding assistant)",
    "weather": "the weather",
    "activities": "outdoor sessions worth planning",
}
PAPERS = {
    "a4": "A4, the paper of most of the world",
    "letter": "US Letter",
}
# Kept in step with tools/documents.py (not imported: it needs the app installed).
LETTER_COUNTRIES = {"US", "CA", "MX", "PH", "CO", "CL", "VE", "CR", "GT", "PA", "DO", "PR"}
GENERATORS = {
    "none": "no generated pictures",
    "gemini": "Google: Gemini's image model and Veo clips, with an AI Studio key",
    "openai": "OpenAI: gpt-image and Sora clips, with an API key",
}
# Calendar and contacts servers that take a password made for one app
# (docs/mail-calendar.md): the CalDAV address, then the CardDAV one.
DAV = {
    "icloud": "iCloud (app-specific password from account.apple.com)",
    "fastmail": "Fastmail (app password with access to calendars and contacts)",
    "nextcloud": "Nextcloud (app password from Settings > Security)",
    "other": "another CalDAV server: you give its address",
}
DAV_URLS = {
    "icloud": ("https://caldav.icloud.com", "https://contacts.icloud.com"),
    "fastmail": ("https://caldav.fastmail.com/dav/", "https://carddav.fastmail.com/dav/"),
}
MAIL = {
    "gmail": "Gmail (app password; 2-Step Verification must be on)",
    "icloud": "iCloud Mail (app-specific password from account.apple.com)",
    "fastmail": "Fastmail (app password with IMAP access)",
    "other": "another IMAP server: you give its host",
}
MAIL_HOSTS = {
    "gmail": "imap.gmail.com",
    "icloud": "imap.mail.me.com",
    "fastmail": "imap.fastmail.com",
}
# Where measured wind can come from (tools/live_wind.py); a test keeps this in step with
# config.LIVE_WIND_SOURCES.
LIVE_SOURCES = {
    "openwindmap": "community wind beacons, no key; dense in France and the Alps",
    "metar": "airports worldwide, no key; a reading every 30 to 60 minutes",
    "meteofrance": "official stations in France; needs a free application id",
}

CHANNELS = {
    "siri": "Siri Shortcuts on iPhone, HomePod, Mac",
    "home_assistant": "Home Assistant Assist (voice satellites, phone app), and through it "
    "Google Home or Alexa routines",
    "telegram": "a chat in Telegram, through a bot you create with @BotFather (nothing else "
    "to run)",
    "whatsapp": "a chat in WhatsApp, through a WAHA gateway you run (an unofficial WhatsApp "
    "client)",
    "push": "push notifications on phone and desktop (ntfy)",
    "scheduled": "a scheduler that calls the briefing endpoint (cron, Home Assistant, or a "
    "scheduled task in a chat or coding-assistant app)",
}
CHATS = {"WHATSAPP_CHAT_ID", "TELEGRAM_CHAT_IDS"}
TELEGRAM_API = "https://api.telegram.org"
SECRET_KEYS = {
    "WAHA_API_KEY",
    "WAHA_DASHBOARD_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "AGENT_API_TOKEN",
    "AUTH_SESSION_SECRET",
    "AUTH_STORAGE_KEY",
    "AUTH_JWT_SECRET",
    "VPN_CONTROL_KEY",
    "TS_AUTHKEY",
    "TUNNEL_TOKEN",
    "WEB_SEARCH_API_KEY",
    "SEARXNG_SECRET",
    "KB_TOKEN",
    "KB_GITHUB_TOKEN",
    "DRIVE_PASSWORD",
    "CODER_TOKEN",
    "JOBS_GITHUB_TOKEN",
    "HUE_APP_KEY",
    "NUKI_API_TOKEN",
    "PETKIT_PASSWORD",
    "HOSTING_API_TOKEN",
    "LLM_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "HA_TOKEN",
    "CALDAV_PASSWORD",
    "MAIL_ACCOUNTS",
    "GITHUB_TOKEN",
    "NTFY_TOKEN",
    "METEOFRANCE_APPLICATION_ID",
    "GENERATION_GEMINI_API_KEY",
    "GENERATION_OPENAI_API_KEY",
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


def add_file(env: dict[str, str], name: str) -> None:
    """One more file of compose/ (by its name, or a path of the user's own)."""
    files = [f for f in env.get("_COMPOSE", "").split(":") if f]
    path = name if "/" in name or name.endswith(".yml") else f"compose/{name}.yml"
    if path not in files:
        env["_COMPOSE"] = ":".join([*files, path])


def compose_files(env: dict[str, str]) -> list[str]:
    """The files picked so far, in the order Compose must read them; a file of the user's
    own (not one of compose/) stays, after the others."""
    picked = [f for f in env.get("_COMPOSE", "").split(":") if f]
    known = [f"compose/{name}.yml" for name in COMPOSE_ORDER]
    return [f for f in known if f in picked] + [f for f in picked if f not in known]


def finish_compose(env: dict[str, str]) -> None:
    files = compose_files(env)
    env.pop("_COMPOSE", None)
    if files:
        # ":" on every system, Windows included.
        env["COMPOSE_PATH_SEPARATOR"] = ":"
        env["COMPOSE_FILE"] = ":".join(["docker-compose.yml", *files])


def reached(env: dict[str, str]) -> str:
    """Where what the agent says by itself arrives, or "" when nothing can carry it."""
    if CHATS & set(env):
        return "in the chat"
    return "as a push" if "NTFY_URL" in env else ""


def ask_identity(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# The assistant")
    env["ASSISTANT_NAME"] = p.ask("Assistant name (what you call it)", "Assistant")
    if user := p.ask("Your first name, as it should call you (empty: no name)"):
        env["USER_NAME"] = user
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
    p.say("\n# 3. The home: where you live (weather, travel time, local search results)")
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
    p.say("\n# 2. The model: who answers?")
    # The subscription right after the API: the two ways to Claude side by side.
    choices = {"anthropic": MODELS["anthropic"], SUBSCRIPTION: SUBSCRIPTION_HELP, **MODELS}
    provider = p.pick("Provider", choices, "anthropic")
    if provider == SUBSCRIPTION:
        ask_subscription(p, env, docker=docker)
        return
    env["LLM_PROVIDER"] = provider
    if provider == "openai-compatible":
        env["LLM_BASE_URL"] = p.ask("Server URL, usually ending in /v1")
    elif provider == "ollama" and docker:
        if p.yes("Run Ollama in Docker next to the agent (no: it already runs here)?", True):
            add_file(env, "ollama")
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


def ask_subscription(p: Prompter, env: dict[str, str], *, docker: bool) -> None:
    env["LLM_PROVIDER"] = "anthropic"
    env["ENGINE"] = "subscription"
    p.say("  Claude Code answers, signed in to your own Claude plan. On a computer with a")
    p.say("  browser, install Claude Code, run `claude setup-token` and paste the token it")
    p.say("  prints (valid a year). Each reply starts a Claude Code process: a few seconds")
    p.say("  more than the API, fine in a chat, slow for a voice. The plan's usage limits")
    p.say("  are shared with your own Claude Code sessions. See docs/subscription.md.")
    if token := p.secret("Token from `claude setup-token`"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    p.say("  An Anthropic API key as well is optional. With one, the API answers what the")
    p.say("  subscription can't (a file you send, a usage limit, an error) and the replies")
    p.say("  you'd rather not wait for. Without one, the subscription answers everything")
    p.say("  and says so when it can't.")
    if key := p.secret("Anthropic API key, to take what the subscription can't"):
        env["LLM_API_KEY"] = key
        displays = p.choose("The subscription answers (the API the rest)", DISPLAYS, ["chat"])
        if displays != ["chat"]:
            env["SUBSCRIPTION_DISPLAYS"] = ",".join(displays)
    env["LLM_MODEL"] = p.ask("Model", "claude-sonnet-5-5")
    # The image leaves Claude Code out unless asked (some 400 MB).
    add_extra(env, "subscription")


def ask_search(p: Prompter, env: dict[str, str], *, docker: bool) -> None:
    if env.get("LLM_PROVIDER", "anthropic") == "anthropic":
        p.say("  Web search: Claude searches with its own tool, nothing to choose.")
        return
    p.say("  Web search: your model writes the answer, a search engine finds the pages.")
    backend = p.pick("Search with", SEARCH, "duckduckgo")
    if backend == "off":
        env["WEB_SEARCH_ENABLED"] = "false"
        return
    env["WEB_SEARCH_BACKEND"] = backend
    if backend == "searxng":
        if docker and p.yes("Run SearXNG in Docker next to the agent?", True):
            add_file(env, "searxng")
            env["SEARXNG_SECRET"] = secrets.token_hex(32)
        else:
            env["WEB_SEARCH_URL"] = p.ask("SearXNG address (JSON format enabled)")
    elif backend in {"brave", "tavily"}:
        if key := p.secret(f"{backend.capitalize()} API key"):
            env["WEB_SEARCH_API_KEY"] = key


def ask_web(p: Prompter, env: dict[str, str], *, docker: bool) -> None:
    p.say("\n# The web: search, pages, investigations (docs/web.md)")
    ask_search(p, env, docker=docker)
    if not p.yes("Let the assistant open public web pages (no logins, no account)?", True):
        env["WEB_PAGES_ENABLED"] = "false"
    if p.yes(
        "Let it investigate a photo, a link or a site (public registries and archives)?", False
    ):
        env["INVESTIGATIONS_ENABLED"] = "true"
        if "AGENT_DOMAIN" in env:
            # Its public address: lets it put a photo online for a reverse image search.
            env["PUBLIC_URL"] = f"https://{env['AGENT_DOMAIN']}"


# How the remote browser's page is reached, per way of reaching the agent: behind a login
# page on a public address, or on the tailnet alone.
BROWSER_ACCESS = {"caddy": "login", "traefik": "login", "tailscale": "tailnet"}


def ask_browser(p: Prompter, env: dict[str, str], access: str) -> None:
    """The remote browser (docs/browser.md), with the Compose files it adds."""
    p.say("\n# Remote browser: sites behind your login (docs/browser.md)")
    p.say("  A real Chrome next to the agent, some 700 MB of memory. You log into a site in")
    p.say("  it yourself, from a link; the agent then opens pages and downloads there.")
    add_file(env, "browser")
    add_extra(env, "browser")
    if BROWSER_ACCESS[access] == "tailnet":
        p.say("  Its page will be served to your Tailscale devices only, on port 8443.")
        tailnet = p.ask("  Your tailnet's DNS name (admin console > DNS), e.g. tail1234.ts.net")
        env["BROWSER_GATE"] = "tailnet"
        env["TS_SERVE"] = "serve-browser"
        host = env.get("TS_HOSTNAME", "home-agent")
        env["BROWSER_PUBLIC_URL"] = f"https://{host}.{tailnet or '<tailnet>'}:8443"
    else:
        labels = env["AGENT_DOMAIN"].split(".")
        parent = ".".join(labels[1:]) if len(labels) > 2 else env["AGENT_DOMAIN"]
        p.say("  Its page sits behind a login page (a passkey, or a password and a code);")
        p.say("  both need a name of their own, with a DNS record to this machine.")
        env["BROWSER_DOMAIN"] = p.ask("  Name for the browser's page", f"browser.{parent}")
        env["AUTH_DOMAIN"] = p.ask("  Name for its login page", f"auth.{parent}")
        env["AUTH_COOKIE_DOMAIN"] = p.ask("  The domain both are under", parent)
        for key in ("AUTH_SESSION_SECRET", "AUTH_STORAGE_KEY", "AUTH_JWT_SECRET"):
            env[key] = secrets.token_hex(32)
        env["BROWSER_PUBLIC_URL"] = f"https://{env['BROWSER_DOMAIN']}"
        add_file(env, "browser-login")
        add_file(env, f"browser-{access}")
    if p.yes("  Also open pages as seen from another country, through a VPN subscription?", False):
        p.say("  Any provider gluetun supports: mullvad, protonvpn, nordvpn, surfshark...")
        if provider := p.ask("  Provider, by gluetun's name for it"):
            env["VPN_SERVICE_PROVIDER"] = provider
            env["VPN_CONTROL_KEY"] = secrets.token_hex(32)
            add_file(env, "vpn")
            p.say("  Add its credentials to .env yourself (docs/browser.md says which).")


def ask_access(p: Prompter, env: dict[str, str]) -> str | None:
    """How clients reach the agent; returns the address they will use, when known."""
    p.say("\n# 4. Reaching the agent: where will clients (phone, speaker) call it from?")
    p.say("  A chat in Telegram or WhatsApp needs none of this: this machine only is enough.")
    access = p.pick("Access", ACCESS, "local")
    env["_ACCESS"] = access
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
        add_file(env, COMPOSE_FILES[access])
    elif env.get("HA_URL") and p.yes(
        "Is Home Assistant on your Tailscale network, out of this machine's direct reach?",
        False,
    ):
        if key := p.secret("Tailscale auth key (admin console > Settings > Keys)"):
            env["TS_AUTHKEY"] = key
        add_file(env, "tailscale")
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
    if "WIND_SPOTS" in env or "SURF_SPOTS" in env:
        p.say("  The wind measured right now near your spots (docs/outdoor.md has the coverage):")
        if sources := p.choose("Read it from (empty: forecasts only)", LIVE_SOURCES, []):
            if "meteofrance" in sources:
                if key := p.secret("Météo-France application id"):
                    env["METEOFRANCE_APPLICATION_ID"] = key
                else:
                    p.say(
                        "  Without it Météo-France stays off: METEOFRANCE_APPLICATION_ID in .env."
                    )
            env["LIVE_WIND_SOURCES"] = ",".join(sources)


def ask_documents(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Documents and pictures (docs/documents.md)")
    p.say("  The assistant can write PDFs (letters, statements), fill forms and draw.")
    usual = "letter" if env.get("HOME_COUNTRY", "").upper() in LETTER_COUNTRIES else "a4"
    env["PAPER_SIZE"] = p.pick("Paper for the PDFs", PAPERS, usual)
    p.say("  A language model writes no photo-like pictures: an image model can, billed")
    p.say("  per picture on an account of yours.")
    provider = p.pick("Generated pictures", GENERATORS, "none")
    if provider != "none" and (key := p.secret(f"API key for {provider}")):
        env[f"GENERATION_{provider.upper()}_API_KEY"] = key
    # The sandbox is the API's: not with a Claude subscription alone.
    sandbox = env.get("LLM_PROVIDER", "anthropic") == "anthropic" and (
        "ENGINE" not in env or "LLM_API_KEY" in env
    )
    if sandbox and p.yes(
        "Exact calculations and charts in Anthropic's code sandbox (billed with the model)?",
        False,
    ):
        env["CODE_EXECUTION_ENABLED"] = "true"


def ask_calendar(p: Prompter, env: dict[str, str]) -> None:
    provider = p.pick("Calendar at", DAV, "icloud")
    if provider == "nextcloud":
        host = p.ask("  Nextcloud address, e.g. https://cloud.example.com").rstrip("/")
        caldav = carddav = f"{host}/remote.php/dav"
    elif provider == "other":
        caldav = p.ask("  CalDAV URL, e.g. https://dav.example.com/")
        carddav = ""
    else:
        caldav, carddav = DAV_URLS[provider]
    env["CALDAV_URL"] = caldav
    env["CALDAV_USERNAME"] = p.ask("  Username (usually your email address)")
    if password := p.secret("  App password (not your account's own password)"):
        env["CALDAV_PASSWORD"] = password
    if p.yes("  Let the agent add events? After it read an email it asks you first.", False):
        env["CALDAV_WRITE"] = "true"
        if name := p.ask("  Calendar for new events (empty: the server's default)"):
            env["CALDAV_DEFAULT_CALENDAR"] = name
    if p.yes("  Show your contacts' birthdays in the agenda (CardDAV, same login)?", False):
        env["CARDDAV_URL"] = carddav or p.ask("  CardDAV URL, e.g. https://dav.example.com/")


def ask_mailbox(p: Prompter, number: int) -> dict[str, object]:
    provider = p.pick("  Mailbox at", MAIL, "other")
    account: dict[str, object] = {
        "name": p.ask(
            "  Short name", provider.capitalize() if provider != "other" else f"Mail{number}"
        ),
        "host": MAIL_HOSTS.get(provider) or p.ask("  IMAP host, e.g. imap.example.com"),
        "username": p.ask("  Username (usually the email address)"),
    }
    account["password"] = p.secret("  App password (not your account's own password)")
    # Reading never marks, moves or deletes anything. Writing is a choice per account.
    if p.yes(
        "  Let the agent save drafts, flag emails and file them into your folders there? "
        "It never sends and never deletes.",
        False,
    ):
        account["write"] = True
    return account


def ask_home_assistant(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# 3. The home: controlling it")
    p.say("  Through Home Assistant, which knows nearly every device. Without it the agent")
    p.say("  still answers, and can drive a few devices by itself (the menu at the end).")
    if p.yes("Control the home through Home Assistant?", True):
        env["HA_URL"] = p.ask("Home Assistant URL", "http://homeassistant.local:8123")
        if token := p.secret("Long-lived access token (HA profile > Security)"):
            env["HA_TOKEN"] = token


def ask_mail(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Mail (docs/mail-calendar.md)")
    p.say("  Any mailbox that offers IMAP with an app password. Read-only unless you agree.")
    accounts = [ask_mailbox(p, 1)]
    while p.yes("Add another mailbox?", False):
        accounts.append(ask_mailbox(p, len(accounts) + 1))
    env["MAIL_ACCOUNTS"] = json.dumps(accounts)


def ask_github(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# GitHub: pull requests, your repositories, notes for coding sessions")
    p.say("  (docs/tasks.md)")
    if "GITHUB_TOKEN" not in env:
        token = p.secret("GitHub token (classic, repo scope)")
        if not token:
            p.say("  Skipped: add GITHUB_TOKEN to .env later.")
            return
        env["GITHUB_TOKEN"] = token
    repo = p.ask("Repo with the coding-assistant reminders, owner/repo (empty: none)")
    if repo:
        env["NOTES_REPO"] = repo
        if p.yes(
            "  Let the agent leave reminders and handoff notes there for your "
            "coding sessions, and close reminders (the token must write to it)?",
            False,
        ):
            env["NOTES_WRITE"] = "true"
    if p.yes(
        "Let the agent read the files of your repositories and check their "
        "dependencies for known flaws (read-only)?",
        False,
    ):
        env["GITHUB_REPOSITORIES"] = "true"


# Devices driven without Home Assistant: what the wizard can take, and for the others
# what to run once .env is written.
DEVICES = {
    "hue": "Philips Hue lights, straight from the bridge",
    "nuki": "a Nuki smart lock (a Nuki Web API token)",
    "petkit": "a PetKit feeder or litter box (a second PetKit account)",
    "netatmo": "Netatmo cameras, weather station or thermostat",
    "android-tv": "a Google TV / Android TV device: a TV, a projector, a box",
    "apple": "an Apple TV, HomePods",
    "devialet": "a Devialet Phantom speaker",
}
# The providers tools/hosting.py speaks to; any other with an MCP server is a connector.
HOSTING = {
    "hostinger": "Hostinger VPS, with their Docker projects (token: hPanel > API)",
    "hetzner": "Hetzner Cloud (token: Cloud Console > the project > Security > API tokens)",
}
DEVICE_STEPS = {
    "netatmo": "run python scripts/netatmo_login.py and add the lines it writes to .env",
    "android-tv": (
        "on the home network, run python scripts/android_tv_pair.py <address>; to let it "
        "find and start a film inside an app (ANDROID_TV_NAVIGATOR), docs/home-devices.md"
    ),
    "apple": "on the home network, run python scripts/apple_devices.py profile <addresses>",
    "withings": "run python scripts/withings_login.py and add the lines it writes to .env",
    "icloud": (
        "log in once, with your Apple ID password and the code your devices show: "
        "docker compose exec agent python -m intelligent_home_agent.icloud_web "
        '(without Docker: pip install ".[icloud]", then the same python -m command); '
        "docs/apple.md says when to do it again"
    ),
}


def add_extra(env: dict[str, str], name: str) -> None:
    """One more optional part of the agent: the Dockerfile's EXTRAS, or without Docker the
    extras `pip install` is told at the end."""
    extras = env.get("EXTRAS", "documents").split(",")
    if name not in extras:
        env["EXTRAS"] = ",".join([*extras, name])


def say_steps(p: Prompter, names: list[str]) -> None:
    steps = [(name, DEVICE_STEPS[name]) for name in names if name in DEVICE_STEPS]
    if steps:
        p.say("  Once .env is written:")
        for name, step in steps:
            p.say(f"  - {name}: {step}.")


def ask_devices(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Home devices, without Home Assistant (docs/home-devices.md)")
    picked = p.choose("Which ones", DEVICES, [])
    if "hue" in picked:
        env["HUE_BRIDGE_URL"] = p.ask("Hue bridge address, e.g. http://192.168.1.50")
        if key := p.secret("App key (docs/home-devices.md shows how to get one)"):
            env["HUE_APP_KEY"] = key
    if "devialet" in picked and (url := p.ask("Speaker address, e.g. http://192.168.1.51")):
        env["DEVIALET_URL"] = url
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
    # The three unofficial libraries are optional parts of the image.
    for name in ("petkit", "android-tv", "apple"):
        if name in picked:
            add_extra(env, name)
    say_steps(p, picked)


def ask_health(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Weigh-ins from a Withings scale, read-only (docs/health.md)")
    say_steps(p, ["withings"])


def ask_apple(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Apple Reminders, Notes and Find My (docs/apple.md)")
    p.say("  Apple has no public API for these apps: the agent uses icloud.com's own,")
    p.say("  with a login you type once and renew when Apple ends it.")
    apple_id = p.ask("  Apple ID (the email of the iCloud account; empty to skip)")
    if not apple_id:
        return
    env["ICLOUD_APPLE_ID"] = apple_id
    if not p.yes("  Read your Notes too?", True):
        env["ICLOUD_NOTES"] = "false"
    if not p.yes("  Find My too (where devices are, battery, a sound)?", True):
        env["ICLOUD_FIND_MY"] = "false"
    add_extra(env, "icloud")
    say_steps(p, ["icloud"])


def ask_hosting(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Your servers at a hosting provider (docs/hosting.md)")
    provider = p.pick("  Hosting provider", HOSTING, "hostinger")
    if token := p.secret("  Its API token"):
        env["HOSTING_PROVIDER"] = provider
        env["HOSTING_API_TOKEN"] = token
    else:
        p.say("  No token: add HOSTING_PROVIDER and HOSTING_API_TOKEN to .env later.")
    p.say("  Another provider with an MCP server: MCP_SERVERS (docs/connectors.md).")


MEMORY = {
    "local": "files on this machine (the agent's data folder); nothing to set up",
    "github": "files in a GitHub repository of yours, readable and editable from anywhere",
    "off": "it remembers nothing between conversations",
}


def ask_memory(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Memory: facts you tell the assistant to remember (docs/memory.md)")
    store = p.pick("Where to keep them", MEMORY, "local")
    if store != "local":
        env["MEMORY_STORE"] = store
    if store != "github":
        return
    p.say("  Any repository works, e.g. the private one claude-code-kit syncs your")
    p.say("  coding-assistant configuration to. The token must be allowed to write to it.")
    while "/" not in (repo := p.ask("Repository, owner/repo")):
        p.say("  Type it as owner/repo.")
    env["MEMORY_REPO"] = repo
    if "GITHUB_TOKEN" not in env:
        if token := p.secret("GitHub token that can write to it (fine-grained, Contents)"):
            env["GITHUB_TOKEN"] = token
    p.say("  If that repository encrypts the folder with git-crypt, add MEMORY_CRYPT_KEY")
    p.say("  to .env afterwards (docs/memory.md).")


def ask_knowledge(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Knowledge base: your own notes, searched by the assistant (docs/knowledge.md)")
    p.say("  Markdown notes in a GitHub repository, e.g. the one claude-code-kit keeps.")
    while "/" not in (repo := p.ask("Notes repository, owner/repo")):
        p.say("  Type it as owner/repo.")
    env["KB_REPO"] = repo
    if token := p.secret("GitHub token that can read it (fine-grained, Contents: read)"):
        env["KB_GITHUB_TOKEN"] = token
    env["KB_TOKEN"] = secrets.token_urlsafe(32)
    add_file(env, "kb")


def ask_drives(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# File drives: the assistant finds, reads and files your documents")
    p.say("  Google Drive, iCloud Drive, OneDrive, Dropbox, a NAS, S3...: anything rclone")
    p.say("  reaches, several at once. docs/drives.md lists them and how each one logs in.")
    drives: list[dict[str, object]] = []
    while True:
        backend = p.ask(
            "Service, by its rclone type (drive = Google Drive, iclouddrive, onedrive, "
            "dropbox, box, pcloud, webdav, smb, sftp, s3...; empty: another one)"
        )
        suggested = DRIVE_NAMES.get(backend, re.sub(r"[^a-z0-9_]", "", backend.lower()))
        taken = {d["name"] for d in drives}
        while not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", name := p.ask("Short name", suggested)) or (
            name in taken
        ):
            p.say("  A new name of lowercase letters, digits or _, starting with a letter.")
        drive: dict[str, object] = {"name": name, "remote": f"{name}:"}
        if backend:
            drive["backend"] = backend
        if not p.yes("May the assistant add, move and delete files there (no: read only)?", True):
            drive["read_only"] = True
        drives.append(drive)
        if not p.yes("Another drive?", False):
            break
    env["DRIVES"] = json.dumps(drives, separators=(",", ":"))
    env["DRIVE_PASSWORD"] = secrets.token_hex(24)
    add_file(env, "drive")
    if env.get("LLM_PROVIDER", "anthropic") == "anthropic" and p.yes(
        "Have the model read scanned PDFs so they can be searched (a paid request each)?", False
    ):
        env["DRIVE_OCR"] = "model"


def telegram_chat(token: str) -> tuple[int, str] | None:
    """The private chat that last wrote to the bot, as (id, first name): asks Telegram
    itself, with the token just typed, and nobody else."""
    try:
        with urllib.request.urlopen(f"{TELEGRAM_API}/bot{token}/getUpdates", timeout=10) as resp:  # noqa: S310
            updates = json.load(resp).get("result") or []
    except (OSError, ValueError):
        return None
    for update in reversed(updates):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") == "private" and isinstance(chat.get("id"), int):
            return chat["id"], str(chat.get("first_name") or "")
    return None


def ask_whatsapp(p: Prompter, env: dict[str, str], *, docker: bool) -> None:
    p.say("  WAHA links to a WhatsApp number as WhatsApp Web does. Linked to your own")
    p.say('  number, the assistant talks in your "Message yourself" chat (it never rings);')
    p.say("  linked to a number of its own, it is a contact like any other.")
    own = p.yes("Does the assistant have a WhatsApp number of its own?", False)
    while not (
        digits := "".join(
            c for c in p.ask("Your WhatsApp number, with its country code") if c.isdigit()
        )
    ):
        p.say("  Digits only, e.g. 15550100 for +1 555 0100.")
    env["WHATSAPP_CHAT_ID"] = f"{digits}@c.us"
    if own:
        # The one session of the gateway is the assistant's account.
        env["AGENT_WAHA_SESSION"] = "default"
    env["WAHA_API_KEY"] = secrets.token_hex(24)
    if docker:
        env["WAHA_DASHBOARD_PASSWORD"] = secrets.token_urlsafe(18)
        add_file(env, "waha")
    else:
        env["WAHA_URL"] = p.ask("Address of your WAHA gateway", "http://127.0.0.1:3000")
        p.say("  Give WAHA the key written in .env as its WAHA_API_KEY.")


def ask_telegram(
    p: Prompter, env: dict[str, str], find_chat: Callable[[str], tuple[int, str] | None]
) -> None:
    p.say("  In Telegram, talk to @BotFather, send /newbot and follow it: it gives a token.")
    token = p.secret("The bot's token")
    if not token:
        p.say("  Skipped: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS to .env later.")
        return
    env["TELEGRAM_BOT_TOKEN"] = token
    p.say("  Only your own chat with the bot may reach the assistant. Open the bot in")
    p.say("  Telegram and send it any message, so that your chat's id can be read.")
    p.ask("Press Enter once it is sent")
    found = find_chat(token)
    if found and p.yes(f"Is this you: {found[1] or 'chat'} (chat {found[0]})?", True):
        env["TELEGRAM_CHAT_IDS"] = str(found[0])
        return
    typed = p.ask("Your chat's id (a number; empty to set TELEGRAM_CHAT_IDS later)")
    if typed.lstrip("-").isdigit():
        env["TELEGRAM_CHAT_IDS"] = typed


def ask_channels(
    p: Prompter,
    env: dict[str, str],
    *,
    docker: bool,
    find_chat: Callable[[str], tuple[int, str] | None] = telegram_chat,
) -> list[str]:
    p.say("\n# 5. Talking to it: how will you talk to the assistant?")
    p.say("  Several can be picked. In a chat you write to it, send files and voice messages")
    p.say("  and approve what it proposes with a tap; a chat or a push is also how it can")
    p.say("  speak first (a briefing, a reminder). Empty: none for now, the API alone.")
    channels = p.choose("Channels", CHANNELS, [])
    if "whatsapp" in channels:
        ask_whatsapp(p, env, docker=docker)
    if "telegram" in channels:
        ask_telegram(p, env, find_chat)
    if {"whatsapp", "telegram"} & set(channels) and p.yes(
        "Understand voice messages, with a speech model on this machine (about 500 MB, and "
        "a few seconds of CPU per message)?",
        False,
    ):
        env["WHISPER_MODEL"] = "small"
        add_extra(env, "voice")
    if "push" in channels:
        suggested = f"https://ntfy.sh/{secrets.token_urlsafe(18)}"
        env["NTFY_URL"] = p.ask("ntfy topic URL (keep it long and random)", suggested)
        if token := p.secret("ntfy access token, for a protected server"):
            env["NTFY_TOKEN"] = token
    return channels


def ask_time(p: Prompter, question: str) -> str:
    while True:
        answer = p.ask(question)
        hours, _, minutes = answer.partition(":")
        if not answer:
            return ""
        if hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60:
            return f"{int(hours):02d}:{int(minutes):02d}"
        p.say("  A time of day as HH:MM, e.g. 07:30; empty for none.")


def ask_tasks(p: Prompter, env: dict[str, str]) -> None:
    """Tasks the assistant runs by itself reach the user in a chat or as a push: asked only
    with one of them."""
    where = reached(env)
    if not where:
        p.say("  Sending the briefing by itself, and reminders, need a chat or a push: none")
        p.say("  is set up (docs/tasks.md).")
        return
    p.say(f"  Tasks the assistant runs by itself are sent {where} (docs/tasks.md).")
    p.say("  You can then ask it to check or remind you of something later, or every week.")
    if at := ask_time(p, "Send the morning briefing every day at (HH:MM, empty: only when asked)"):
        env["MORNING_BRIEFING_AT"] = at
    if at := ask_time(p, "Send the evening recap every day at (HH:MM, empty: only when asked)"):
        env["EVENING_RECAP_AT"] = at
    if "NOTES_REPO" in env and p.yes(
        f"Keep the tasks in {env['NOTES_REPO']} rather than on this machine, so you (and "
        "your coding assistant) can write some there?",
        False,
    ):
        env["TASKS_STORE"] = "github"


def ask_hour(p: Prompter, question: str, default: int) -> int:
    while True:
        hour = p.number(question, str(default))
        if hour == int(hour) and 0 <= hour <= 23:
            return int(hour)
        p.say("  An hour of the day, 0 to 23.")


def ask_activity_alerts(p: Prompter, env: dict[str, str]) -> None:
    """A message when a good window is coming for an activity: asked only when something
    can carry it (a push) and for the activities that can be judged."""
    chosen = [k for k in env.get("OUTDOOR_ACTIVITIES", "").split(",") if k]
    needs = {"wind": "WIND_SPOTS", "surf": "SURF_SPOTS", "weather": "HOME_LATITUDE"}
    possible = [k for k in chosen if needs[CATALOG[k].kind] in env]
    where = reached(env)
    if not where or not possible:
        return
    p.say(f"  Alerts for your activities, sent {where} (docs/outdoor.md):")
    p.say("  One message when a good window is coming (enough wind at a spot, waves at a")
    p.say("  beach, a dry and mild stretch for the rest); nothing when nothing fits. You can")
    p.say("  also just ask the assistant later: 'tell me when it's good for a ride'.")
    labels = {k: ", ".join(FORECASTS[n] for n in CATALOG[k].needs) for k in possible}
    picked = p.choose("Alert you for (empty: none)", labels, [])
    if not picked:
        return
    env["ACTIVITY_ALERTS"] = ",".join(picked)
    while not 1 <= (lead := p.number("How many hours ahead, at most (1 to 72)", "24")) <= 72:
        p.say("  Between 1 and 72 hours.")
    env["ACTIVITY_ALERTS_LEAD_HOURS"] = str(int(lead))
    env["ACTIVITY_ALERTS_QUIET_START"] = str(ask_hour(p, "No message from (hour, 0-23)", 22))
    env["ACTIVITY_ALERTS_QUIET_END"] = str(ask_hour(p, "... until (hour, 0-23)", 8))


def ask_jobs(p: Prompter, env: dict[str, str]) -> None:
    """Coding jobs report in a chat or as a push, and their coder is a container of the
    stack: on offer only with both. They are for people who code."""
    where = reached(env)
    if not where:
        p.say("  Coding jobs report in a chat or as a push: none is set up (docs/jobs.md).")
        return
    p.say("\n# Coding jobs, for people who code (docs/jobs.md)")
    p.say("  Ask the assistant for a fix or a feature on one of your GitHub repositories:")
    p.say("  Claude Code does it in a container of its own, on your Claude subscription,")
    p.say(f"  opens a pull request and reports {where}. Needs a Claude plan and GitHub.")
    env["JOBS_RUNNER"] = "claude_code"
    env["CODER_TOKEN"] = secrets.token_urlsafe(32)
    p.say("  The coder can touch what its GitHub token can: make a fine-grained one, limited")
    p.say("  to the repositories it may work on (Contents and Pull requests: read and write).")
    if token := p.secret("That GitHub token"):
        env["JOBS_GITHUB_TOKEN"] = token
    if name := p.ask("Name on the coder's commits", env.get("USER_NAME", "")):
        env["JOBS_GIT_NAME"] = name
    if email := p.ask("Email on the coder's commits (the one of your GitHub account)"):
        env["JOBS_GIT_EMAIL"] = email
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        p.say("  The coder signs in to your Claude plan: on a computer with a browser, install")
        p.say("  Claude Code and run `claude setup-token` (the token is valid a year).")
        if token := p.secret("Token from `claude setup-token`"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    add_file(env, "coder")
    p.say("  Nightly review (docs/review.md): each night a coding job can read the day's")
    p.say("  journal, which holds what you asked the assistant, and open a pull request")
    p.say("  that fixes what made it fail, on YOUR OWN private copy of this repository.")
    if p.yes("Turn the nightly review on?", False):
        while "/" not in (
            repo := p.ask("Your private copy, owner/repo (the token above must push)")
        ):
            p.say("  Type it as owner/repo.")
        env["REVIEW_REPO"] = repo


# What the wizard writes, in the order of docs/setup.md and .env.example.
GROUPS = [
    ("API", ["AGENT_API_TOKEN"]),
    (
        "Docker Compose: the containers in use and the optional parts of the image",
        ["COMPOSE_PATH_SEPARATOR", "COMPOSE_FILE", "EXTRAS"],
    ),
    (
        "The model (docs/setup.md, step 2)",
        ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"],
    ),
    (
        "Claude on a Claude subscription (docs/subscription.md)",
        ["ENGINE", "CLAUDE_CODE_OAUTH_TOKEN", "SUBSCRIPTION_DISPLAYS"],
    ),
    ("The assistant", ["ASSISTANT_NAME", "USER_NAME", "LANGUAGE", "TIMEZONE"]),
    (
        "The home (docs/setup.md, step 3)",
        ["HOME_CITY", "HOME_COUNTRY", "HOME_LATITUDE", "HOME_LONGITUDE", "HA_URL", "HA_TOKEN"],
    ),
    (
        "Reaching the agent (docs/setup.md, step 4)",
        [
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
    (
        "Talking to it (docs/setup.md, step 5; docs/chat.md)",
        [
            "NTFY_URL",
            "NTFY_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_IDS",
            "WAHA_URL",
            "WAHA_API_KEY",
            "WAHA_DASHBOARD_PASSWORD",
            "AGENT_WAHA_SESSION",
            "WHATSAPP_CHAT_ID",
            "WHISPER_MODEL",
            "BRIEF_SECTIONS",
            "WEATHER_DETAIL",
        ],
    ),
    (
        "Home devices (docs/home-devices.md)",
        [
            "HUE_BRIDGE_URL",
            "HUE_APP_KEY",
            "DEVIALET_URL",
            "NUKI_API_TOKEN",
            "PETKIT_USERNAME",
            "PETKIT_PASSWORD",
            "PETKIT_REGION",
        ],
    ),
    (
        "Mail and calendar (docs/mail-calendar.md)",
        [
            "MAIL_ACCOUNTS",
            "CALDAV_URL",
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_WRITE",
            "CALDAV_DEFAULT_CALENDAR",
            "CARDDAV_URL",
        ],
    ),
    (
        "Files and documents (docs/drives.md, docs/documents.md)",
        [
            "DRIVE_PASSWORD",
            "DRIVES",
            "DRIVE_OCR",
            "PAPER_SIZE",
            "GENERATION_GEMINI_API_KEY",
            "GENERATION_OPENAI_API_KEY",
            "CODE_EXECUTION_ENABLED",
        ],
    ),
    (
        "The web (docs/web.md)",
        [
            "WEB_SEARCH_ENABLED",
            "WEB_SEARCH_BACKEND",
            "WEB_SEARCH_URL",
            "WEB_SEARCH_API_KEY",
            "SEARXNG_SECRET",
            "WEB_PAGES_ENABLED",
            "INVESTIGATIONS_ENABLED",
            "PUBLIC_URL",
        ],
    ),
    (
        "Memory and knowledge (docs/memory.md, docs/knowledge.md)",
        ["MEMORY_STORE", "MEMORY_REPO", "KB_REPO", "KB_GITHUB_TOKEN", "KB_TOKEN"],
    ),
    (
        "Tasks and alerts (docs/tasks.md)",
        [
            "TASKS_STORE",
            "MORNING_BRIEFING_AT",
            "EVENING_RECAP_AT",
            "GITHUB_TOKEN",
            "GITHUB_REPOSITORIES",
            "NOTES_REPO",
            "NOTES_WRITE",
        ],
    ),
    ("Servers at a hosting provider (docs/hosting.md)", ["HOSTING_PROVIDER", "HOSTING_API_TOKEN"]),
    (
        "Apple Reminders, Notes and Find My (docs/apple.md)",
        ["ICLOUD_APPLE_ID", "ICLOUD_NOTES", "ICLOUD_FIND_MY"],
    ),
    (
        "Remote browser (docs/browser.md)",
        [
            "BROWSER_PUBLIC_URL",
            "BROWSER_GATE",
            "BROWSER_DOMAIN",
            "AUTH_DOMAIN",
            "AUTH_COOKIE_DOMAIN",
            "AUTH_SESSION_SECRET",
            "AUTH_STORAGE_KEY",
            "AUTH_JWT_SECRET",
            "VPN_SERVICE_PROVIDER",
            "VPN_CONTROL_KEY",
        ],
    ),
    (
        "Coding jobs and the nightly review (docs/jobs.md, docs/review.md)",
        [
            "JOBS_RUNNER",
            "CODER_TOKEN",
            "JOBS_GITHUB_TOKEN",
            "JOBS_GIT_NAME",
            "JOBS_GIT_EMAIL",
            "REVIEW_REPO",
        ],
    ),
    (
        "Outdoor activities (docs/outdoor.md)",
        [
            "OUTDOOR_ACTIVITIES",
            "WIND_SPOTS",
            "SURF_SPOTS",
            "LIVE_WIND_SOURCES",
            "METEOFRANCE_APPLICATION_ID",
            "ACTIVITY_ALERTS",
            "ACTIVITY_ALERTS_LEAD_HOURS",
            "ACTIVITY_ALERTS_QUIET_START",
            "ACTIVITY_ALERTS_QUIET_END",
        ],
    ),
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


def load_env(path: Path) -> dict[str, str]:
    """The settings of an existing .env; an empty value is a setting not made."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if found := re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line):
            env[found.group(1)] = found.group(2).strip()
    return {key: value for key, value in env.items() if value}


def merge_env(text: str, changed: dict[str, str]) -> str:
    """The .env with these settings changed where they are, or added at the end; every
    other line stays as it was."""
    lines = text.splitlines()
    added = []
    for key, value in changed.items():
        hits = [i for i, line in enumerate(lines) if line.startswith(f"{key}=")]
        if hits:
            lines[hits[-1]] = f"{key}={value}"  # the last one is the one that counts
        else:
            added.append(f"{key}={value}")
    if added:
        lines += ["", "# Added by scripts/setup.py", *added]
    return "\n".join(lines) + "\n"


def access_of(env: dict[str, str]) -> str:
    """How the agent of an existing .env is reached, from its Compose files."""
    files = env.get("COMPOSE_FILE", "")
    for access in ("caddy", "traefik"):
        if f"compose/{access}.yml" in files:
            return access
    if "compose/tailscale.yml" in files:
        return "funnel" if env.get("TS_SERVE") == "funnel" else "tailscale"
    if "compose/cloudflared.yml" in files:
        return "cloudflare"
    return "lan" if "AGENT_BIND" in env else "local"


# The optional features, in the order of docs/setup.md ("Then add what you want"). The
# order is also the one they are asked in: GitHub before the briefing, which may keep its
# tasks in the notes repository.
FEATURES = {
    "devices": "home devices without Home Assistant: Hue, a lock, cameras, a TV, a pet feeder",
    "health": "weigh-ins from a Withings scale",
    "mail": "your mailboxes (IMAP): read; drafts and filing if you allow it",
    "calendar": "your calendar (CalDAV) and contacts' birthdays",
    "drives": "file drives: Google Drive, iCloud Drive, OneDrive, Dropbox, a NAS...",
    "documents": "PDFs it writes: the paper size; generated pictures; a code sandbox",
    "web": "web search engine, web pages, investigations (search and pages are on anyway)",
    "memory": "where its memory is kept (default: files on this machine) or none",
    "knowledge": "a knowledge base: your Markdown notes in a GitHub repository",
    "github": "GitHub: pull requests waiting for you, notes for your coding sessions",
    "briefing": "the briefing and the recap: what they cover, sending them every day",
    "hosting": "your servers at a hosting provider (Hostinger, Hetzner Cloud)",
    "apple": "Apple Reminders, Notes and Find My, through your iCloud account",
    "browser": "a remote browser, for sites behind your login",
    "jobs": "coding jobs on your GitHub repositories, and the nightly review",
    "outdoor": "outdoor activities: wind, surf, weather windows, alerts",
}
# Why a feature is not on offer, by what it lacks.
NEEDS = {
    "docker": "needs Docker Compose",
    "access": "needs the agent reached through Caddy, Traefik or Tailscale",
    "reach": "needs a chat or a push, so that reports can reach you",
}


def features_on_offer(
    env: dict[str, str], *, docker: bool, access: str
) -> tuple[dict[str, str], dict[str, str]]:
    """The features that can be set up here, and for the others what is missing."""
    missing: dict[str, str] = {}
    if not docker:
        missing = dict.fromkeys(("drives", "knowledge", "browser", "jobs"), NEEDS["docker"])
    else:
        if access not in BROWSER_ACCESS:
            missing["browser"] = NEEDS["access"]
        if not reached(env):
            missing["jobs"] = NEEDS["reach"]
    return {k: v for k, v in FEATURES.items() if k not in missing}, missing


def ask_features(
    p: Prompter,
    env: dict[str, str],
    *,
    docker: bool,
    access: str,
) -> list[str]:
    """One menu for everything optional; each pick leads to its own questions."""
    options, missing = features_on_offer(env, docker=docker, access=access)
    p.say("\n# What else do you want to set up now?")
    p.say("  All optional. Whatever you leave out can be added later: run this wizard")
    p.say("  again and choose to add, or see docs/setup.md, 'Then add what you want'.")
    picked = p.choose("Set up now (empty: nothing more)", options, [])
    for name, why in missing.items():
        p.say(f"  Not offered here: {name} ({why}).")

    def briefing() -> None:
        ask_brief(p, env)
        ask_tasks(p, env)

    def outdoor() -> None:
        ask_activities(p, env)
        ask_activity_alerts(p, env)

    steps: dict[str, Callable[[], None]] = {
        "devices": lambda: ask_devices(p, env),
        "health": lambda: ask_health(p, env),
        "mail": lambda: ask_mail(p, env),
        "calendar": lambda: ask_calendar(p, env),
        "drives": lambda: ask_drives(p, env),
        "documents": lambda: ask_documents(p, env),
        "web": lambda: ask_web(p, env, docker=docker),
        "memory": lambda: ask_memory(p, env),
        "knowledge": lambda: ask_knowledge(p, env),
        "github": lambda: ask_github(p, env),
        "briefing": briefing,
        "hosting": lambda: ask_hosting(p, env),
        "apple": lambda: ask_apple(p, env),
        "browser": lambda: ask_browser(p, env, access),
        "jobs": lambda: ask_jobs(p, env),
        "outdoor": outdoor,
    }
    for name in options:
        if name in picked:
            steps[name]()
    return picked


def finish(env: dict[str, str], *, docker: bool) -> str:
    """Turns what was picked into the Compose lines; without Docker, returns the optional
    parts to install with pip instead."""
    env.pop("_ACCESS", None)
    if docker:
        finish_compose(env)
        return ""
    env.pop("_COMPOSE", None)
    return env.pop("EXTRAS", "documents")


NEXT_STEPS = {
    "siri": "Siri: python scripts/build_shortcuts.py on a Mac (docs/siri-shortcut.md).",
    "home_assistant": "Home Assistant, Google Home, Alexa: docs/integrations.md.",
    "telegram": "Telegram: write to your bot once the agent runs (docs/chat.md).",
    "whatsapp": "WhatsApp: link the number in WAHA's dashboard, then write in the chat "
    "(docs/chat.md).",
    "push": "ntfy: install the app and subscribe to the topic URL in .env (NTFY_URL).",
    "scheduled": "Scheduled briefings: POST /v1/briefing (docs/integrations.md).",
}


def closing(
    p: Prompter,
    env: dict[str, str],
    *,
    docker: bool,
    extras: str,
    address: str | None = None,
    channels: tuple[str, ...] | list[str] = (),
) -> None:
    """What is still missing, and what to do next."""
    files = env.get("COMPOSE_FILE", "")
    provider = env.get("LLM_PROVIDER", "anthropic")
    missing = [
        key
        for key, needed in (
            ("LLM_API_KEY", provider not in KEYLESS and "ENGINE" not in env),
            ("CLAUDE_CODE_OAUTH_TOKEN", "ENGINE" in env or "JOBS_RUNNER" in env),
            ("JOBS_GITHUB_TOKEN", "JOBS_RUNNER" in env),
            ("TELEGRAM_CHAT_IDS", "TELEGRAM_BOT_TOKEN" in env),
            ("TS_AUTHKEY", "compose/tailscale.yml" in files),
            ("TUNNEL_TOKEN", "compose/cloudflared.yml" in files),
            ("WEB_SEARCH_API_KEY", env.get("WEB_SEARCH_BACKEND") in {"brave", "tavily"}),
            ("KB_GITHUB_TOKEN", "KB_REPO" in env),
            ("GITHUB_TOKEN", env.get("MEMORY_STORE") == "github"),
        )
        if needed and key not in env
    ]
    if missing:
        p.say(f"\nStill empty, add to .env before starting: {', '.join(missing)}.")
    p.say("\nNext steps:")
    if docker:
        p.say("  Start: docker compose up -d --build")
        if "compose/ollama.yml" in files:
            p.say(f"  Then: docker compose exec ollama ollama pull {env['LLM_MODEL']}")
        if address:
            p.say(f"  The agent will answer at {address}")
        if "BROWSER_DOMAIN" in env:
            p.say("  Remote browser: scripts/browser-auth.sh user, before the first login.")
        if "DRIVES" in env:
            p.say("  Drives: log each one in before asking about your files (docs/drives.md).")
    else:
        p.say(f'  Start: pip install ".[{extras}]" && uvicorn intelligent_home_agent.main:app')
    p.say("  Check: docs/setup.md, step 6. Clients authenticate with AGENT_API_TOKEN from .env.")
    for channel in channels:
        p.say(f"  {NEXT_STEPS[channel]}")
    p.say("  Later: run this wizard again to add a feature. Your bank or any service with an")
    p.say("  MCP server is set by hand: docs/connectors.md.")


EXISTING = {
    "add": "keep it and add features to it",
    "replace": "start again and replace it",
    "keep": "leave it alone and stop here",
}


def add_features(p: Prompter, path: Path, existing: dict[str, str], *, docker: bool) -> bool:
    """The menu alone, on a .env that is already there: only what changes is written."""
    env = dict(existing)
    for file in existing.get("COMPOSE_FILE", "").split(":"):
        if file and file != "docker-compose.yml":
            add_file(env, file)
    ask_features(p, env, docker=docker, access=access_of(existing))
    extras = finish(env, docker=docker)
    changed = {k: v for k, v in env.items() if existing.get(k) != v}
    if not changed:
        p.say("Nothing to change.")
        return False
    merged = merge_env(path.read_text(), changed)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(merged)
    os.chmod(path, 0o600)
    p.say(f"\nUpdated {path}:")
    for line in summary(changed):
        p.say(line)
    closing(p, env, docker=docker, extras=extras)
    return True


def run(
    p: Prompter,
    path: Path,
    lookup: Callable[[str, str], dict | None] = geocode,
    find_chat: Callable[[str], tuple[int, str] | None] = telegram_chat,
) -> bool:
    p.say("intelligent-home-agent setup. Press Enter to keep the value in brackets.")
    existing = None
    if path.exists():
        choice = p.pick(f"{path} exists", EXISTING, "add")
        if choice == "keep":
            p.say("Nothing written.")
            return False
        if choice == "add":
            existing = load_env(path)
    p.say("\n# 1. Where it runs")
    docker = p.yes("Will the agent run with Docker Compose (no: straight with Python)?", True)
    if existing is not None:
        return add_features(p, path, existing, docker=docker)

    env: dict[str, str] = {"AGENT_API_TOKEN": secrets.token_urlsafe(32)}
    ask_identity(p, env)
    ask_model(p, env, docker=docker)
    ask_home(p, env, lookup)
    ask_home_assistant(p, env)
    address = ask_access(p, env) if docker else None
    channels = ask_channels(p, env, docker=docker, find_chat=find_chat)
    ask_features(p, env, docker=docker, access=env.get("_ACCESS", "local"))
    extras = finish(env, docker=docker)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render(env))
    os.chmod(path, 0o600)
    p.say(f"\nWrote {path} (readable by you only):")
    for line in summary(env):
        p.say(line)
    closing(p, env, docker=docker, extras=extras, address=address, channels=channels)
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
