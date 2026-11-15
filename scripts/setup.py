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
CHANNELS = {
    "siri": "Siri Shortcuts on iPhone, HomePod, Mac",
    "home_assistant": "Home Assistant Assist (voice satellites, phone app), and through it "
    "Google Home or Alexa routines",
    "push": "push notifications on phone and desktop (ntfy)",
    "scheduled": "a scheduler that calls the briefing endpoint (cron, Home Assistant, or a "
    "scheduled task in a chat or coding-assistant app)",
}
CHAT = {
    "none": "no chat: voice, push notifications or your own client only",
    "whatsapp": "WhatsApp, through a WAHA gateway you run (an unofficial WhatsApp client)",
    "telegram": "Telegram, through a bot you create with @BotFather (nothing else to run)",
    "both": "WhatsApp and Telegram",
}
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


def ask_identity(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Assistant")
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
    if docker:
        # The image leaves Claude Code out unless asked (some 400 MB).
        env["EXTRAS"] = "documents,subscription"


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


def ask_web(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Web pages and investigations (docs/web.md)")
    if not p.yes("Let the assistant open public web pages (no logins, no account)?", True):
        env["WEB_PAGES_ENABLED"] = "false"
    if p.yes(
        "Let it investigate a photo, a link or a site (public registries and archives)?", False
    ):
        env["INVESTIGATIONS_ENABLED"] = "true"


# How the remote browser's page is reached, per way of reaching the agent: behind a login
# page on a public address, or on the tailnet alone.
BROWSER_ACCESS = {"caddy": "login", "traefik": "login", "tailscale": "tailnet"}


def ask_browser(p: Prompter, env: dict[str, str], access: str) -> list[str]:
    """The remote browser (docs/browser.md); returns the Compose files it adds."""
    if not p.yes(
        "Add a remote browser, for sites behind your login (docs/browser.md; some 700 MB "
        "of memory)?",
        False,
    ):
        return []
    files = ["compose/browser.yml"]
    add_extra(env, "browser")
    if BROWSER_ACCESS[access] == "tailnet":
        p.say("  Its page will be served to your Tailscale devices only, on port 8443.")
        tailnet = p.ask("  Your tailnet's DNS name (admin console > DNS), e.g. tail1234.ts.net")
        env["BROWSER_GATE"] = "tailnet"
        env["TS_SERVE"] = "serve-browser"
        env["BROWSER_PUBLIC_URL"] = f"https://{env['TS_HOSTNAME']}.{tailnet or '<tailnet>'}:8443"
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
        files += ["compose/browser-login.yml", f"compose/browser-{access}.yml"]
    if p.yes("  Also open pages as seen from another country, through a VPN subscription?", False):
        p.say("  Any provider gluetun supports: mullvad, protonvpn, nordvpn, surfshark...")
        if provider := p.ask("  Provider, by gluetun's name for it"):
            env["VPN_SERVICE_PROVIDER"] = provider
            env["VPN_CONTROL_KEY"] = secrets.token_hex(32)
            files.append("compose/vpn.yml")
            p.say("  Add its credentials to .env yourself (docs/browser.md says which).")
    return files


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
        if "INVESTIGATIONS_ENABLED" in env:
            # Its public address: lets it put a photo online for a reverse image search.
            env["PUBLIC_URL"] = address
        if access == "traefik":
            env["TRAEFIK_NETWORK"] = p.ask("Docker network Traefik shares with services", "proxy")
            env["TRAEFIK_CERTRESOLVER"] = p.ask("Traefik certificate resolver", "letsencrypt")
    if access in COMPOSE_FILES:
        files.append(f"compose/{COMPOSE_FILES[access]}.yml")
    if access in BROWSER_ACCESS:
        files += ask_browser(p, env, access)
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
    if env.pop("_COMPOSE_DRIVE", None):
        files.append("compose/drive.yml")
    if env.pop("_COMPOSE_WAHA", None):
        files.append("compose/waha.yml")
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
    sandbox = env.get("LLM_PROVIDER") == "anthropic" and (
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
    if "calendar" in sections and p.yes("Read a calendar over CalDAV?", True):
        ask_calendar(p, env)
    if "mail" in sections:
        accounts = []
        while p.yes("Add a mailbox (IMAP)?", not accounts):
            accounts.append(ask_mailbox(p, len(accounts) + 1))
        if accounts:
            env["MAIL_ACCOUNTS"] = json.dumps(accounts)
    if "pull_requests" in sections or "reminders" in sections:
        if token := p.secret("GitHub token (classic, repo scope) for pull requests and reminders"):
            env["GITHUB_TOKEN"] = token
            if "reminders" in sections:
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


# Devices and services set up beyond the wizard: what each needs once .env is written.
DEVICES = {
    "nuki": "a Nuki smart lock (a Nuki Web API token)",
    "petkit": "a PetKit feeder or litter box (a second PetKit account)",
    "netatmo": "Netatmo cameras, weather station or thermostat",
    "android-tv": "a Google TV / Android TV device: a TV, a projector, a box",
    "apple": "an Apple TV, HomePods",
    "withings": "a Withings scale (weigh-ins, read-only)",
    "icloud": "Apple Reminders, Notes and Find My, through your iCloud account",
    "hosting": "your servers at a hosting provider (state, metrics, restarts on your yes)",
}
# The providers tools/hosting.py speaks to; any other with an MCP server is a connector.
HOSTING = {
    "hostinger": "Hostinger VPS, with their Docker projects (token: hPanel > API)",
    "hetzner": "Hetzner Cloud (token: Cloud Console > the project > Security > API tokens)",
}
DEVICE_STEPS = {
    "petkit": 'install the library: pip install ".[petkit]" (Docker: docs/home-devices.md)',
    "netatmo": "run python scripts/netatmo_login.py and add the lines it writes to .env",
    "android-tv": "on the home network, run python scripts/android_tv_pair.py <address>",
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
    """One more optional part of the Docker image (the Dockerfile's EXTRAS)."""
    extras = env.get("EXTRAS", "documents").split(",")
    if name not in extras:
        env["EXTRAS"] = ",".join([*extras, name])


def ask_devices(p: Prompter, env: dict[str, str], *, docker: bool = True) -> None:
    p.say("\n# More of the home, without Home Assistant (docs/home-devices.md, docs/health.md)")
    if not p.yes(
        "Set up a lock, cameras, a pet feeder, a TV, Apple devices, a scale, Apple "
        "Reminders, Notes and Find My, or your servers at a hosting provider?",
        False,
    ):
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
    if "icloud" in picked:
        p.say("  Apple has no public API for these apps: the agent uses icloud.com's own,")
        p.say("  with a login you type once and renew when Apple ends it (docs/apple.md).")
        if apple_id := p.ask("  Apple ID (the email of the iCloud account)"):
            env["ICLOUD_APPLE_ID"] = apple_id
            if not p.yes("  Read your Notes too?", True):
                env["ICLOUD_NOTES"] = "false"
            if not p.yes("  Find My too (where devices are, battery, a sound)?", True):
                env["ICLOUD_FIND_MY"] = "false"
            if docker:
                add_extra(env, "icloud")
        else:
            picked.remove("icloud")
    if "hosting" in picked:
        provider = p.pick("  Hosting provider", HOSTING, "hostinger")
        if token := p.secret("  Its API token (docs/hosting.md)"):
            env["HOSTING_PROVIDER"] = provider
            env["HOSTING_API_TOKEN"] = token
        else:
            p.say("  No token: add HOSTING_PROVIDER and HOSTING_API_TOKEN to .env later.")
        p.say("  Another provider with an MCP server: MCP_SERVERS (docs/connectors.md).")
    steps = [(name, DEVICE_STEPS[name]) for name in picked if name in DEVICE_STEPS]
    if steps:
        p.say("  Once .env is written:")
        for name, step in steps:
            p.say(f"  - {name}: {step}.")


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
    if not p.yes("Use one?", False):
        return
    while "/" not in (repo := p.ask("Notes repository, owner/repo")):
        p.say("  Type it as owner/repo.")
    env["KB_REPO"] = repo
    if token := p.secret("GitHub token that can read it (fine-grained, Contents: read)"):
        env["KB_GITHUB_TOKEN"] = token
    env["KB_TOKEN"] = secrets.token_urlsafe(32)
    env["_COMPOSE_KB"] = "1"


def ask_drives(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# File drives: the assistant finds, reads and files your documents")
    p.say("  Google Drive, iCloud Drive, OneDrive, Dropbox, a NAS, S3...: anything rclone")
    p.say("  reaches, several at once. docs/drives.md lists them and how each one logs in.")
    if not p.yes("Use one?", False):
        return
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
    env["_COMPOSE_DRIVE"] = "1"
    if env["LLM_PROVIDER"] == "anthropic" and p.yes(
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
        env["_COMPOSE_WAHA"] = "1"
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


def ask_chat(
    p: Prompter,
    env: dict[str, str],
    *,
    docker: bool,
    find_chat: Callable[[str], tuple[int, str] | None] = telegram_chat,
) -> None:
    p.say("\n# A chat with the assistant in a messaging app (docs/chat.md)")
    p.say("  Write to it, send it files and voice messages, approve what it proposes with a")
    p.say("  tap; its briefings and the tasks it runs by itself arrive there.")
    chat = p.pick("Chat", CHAT, "none")
    if chat == "none":
        return
    if chat in {"whatsapp", "both"}:
        ask_whatsapp(p, env, docker=docker)
    if chat in {"telegram", "both"}:
        ask_telegram(p, env, find_chat)
    if p.yes(
        "Understand voice messages, with a speech model on this machine (about 500 MB, and "
        "a few seconds of CPU per message)?",
        False,
    ):
        env["WHISPER_MODEL"] = "small"
        if docker:
            env["EXTRAS"] = f"{env.get('EXTRAS', 'documents')},voice"
        else:
            p.say('  Install it with: pip install ".[voice]"')


def ask_channels(p: Prompter, env: dict[str, str]) -> list[str]:
    p.say("\n# How will you reach the assistant?")
    channels = p.choose("Channels", CHANNELS, ["siri"])
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
    if not {"NTFY_URL", "WHATSAPP_CHAT_ID", "TELEGRAM_CHAT_IDS"} & set(env):
        return
    where = "in the chat" if {"WHATSAPP_CHAT_ID", "TELEGRAM_CHAT_IDS"} & set(env) else "as a push"
    p.say(f"\n# Tasks the assistant runs by itself, sent {where} (docs/tasks.md)")
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


GROUPS = [
    ("API", ["AGENT_API_TOKEN"]),
    (
        "Docker Compose: how the agent is reached (docs/setup.md)",
        [
            "COMPOSE_PATH_SEPARATOR",
            "COMPOSE_FILE",
            "EXTRAS",
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
    ("Claude subscription", ["ENGINE", "CLAUDE_CODE_OAUTH_TOKEN", "SUBSCRIPTION_DISPLAYS"]),
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
    ("Web pages and investigations", ["WEB_PAGES_ENABLED", "INVESTIGATIONS_ENABLED", "PUBLIC_URL"]),
    ("Assistant", ["ASSISTANT_NAME", "USER_NAME", "LANGUAGE", "TIMEZONE"]),
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
    (
        "Calendar",
        [
            "CALDAV_URL",
            "CALDAV_USERNAME",
            "CALDAV_PASSWORD",
            "CALDAV_WRITE",
            "CALDAV_DEFAULT_CALENDAR",
            "CARDDAV_URL",
        ],
    ),
    (
        "Apple Reminders, Notes and Find My (docs/apple.md)",
        ["ICLOUD_APPLE_ID", "ICLOUD_NOTES", "ICLOUD_FIND_MY"],
    ),
    ("Mail", ["MAIL_ACCOUNTS"]),
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
    ("GitHub", ["GITHUB_TOKEN", "GITHUB_REPOSITORIES", "NOTES_REPO", "NOTES_WRITE"]),
    ("Servers at a hosting provider", ["HOSTING_PROVIDER", "HOSTING_API_TOKEN"]),
    (
        "Documents and pictures",
        [
            "PAPER_SIZE",
            "GENERATION_GEMINI_API_KEY",
            "GENERATION_OPENAI_API_KEY",
            "CODE_EXECUTION_ENABLED",
        ],
    ),
    ("Memory", ["MEMORY_STORE", "MEMORY_REPO"]),
    ("Knowledge base", ["KB_REPO", "KB_GITHUB_TOKEN", "KB_TOKEN"]),
    ("File drives", ["DRIVE_PASSWORD", "DRIVES", "DRIVE_OCR"]),
    ("Push", ["NTFY_URL", "NTFY_TOKEN"]),
    (
        "Chat channels (docs/chat.md)",
        [
            "WAHA_URL",
            "WAHA_API_KEY",
            "WAHA_DASHBOARD_PASSWORD",
            "AGENT_WAHA_SESSION",
            "WHATSAPP_CHAT_ID",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_IDS",
            "WHISPER_MODEL",
        ],
    ),
    ("Tasks", ["TASKS_STORE", "MORNING_BRIEFING_AT", "EVENING_RECAP_AT"]),
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


def run(
    p: Prompter,
    path: Path,
    lookup: Callable[[str, str], dict | None] = geocode,
    find_chat: Callable[[str], tuple[int, str] | None] = telegram_chat,
) -> bool:
    p.say("intelligent-home-agent setup. Press Enter to keep the value in brackets.")
    if path.exists() and not p.yes(f"{path} exists. Replace it?", False):
        p.say("Nothing written.")
        return False
    env: dict[str, str] = {"AGENT_API_TOKEN": secrets.token_urlsafe(32)}
    docker = p.yes("Will the agent run with Docker Compose (no: straight with Python)?", True)
    ask_identity(p, env)
    ask_model(p, env, docker=docker)
    ask_search(p, env, docker=docker)
    ask_web(p, env)
    ask_home(p, env, lookup)
    sections = ask_brief(p, env)
    ask_activities(p, env)
    ask_documents(p, env)
    ask_sources(p, env, sections)
    ask_devices(p, env, docker=docker)
    ask_memory(p, env)
    if docker:
        ask_knowledge(p, env)
        ask_drives(p, env)
    ask_chat(p, env, docker=docker, find_chat=find_chat)
    address = ask_access(p, env) if docker else None
    env.pop("_COMPOSE_WAHA", None)
    env.pop("_COMPOSE_KB", None)
    env.pop("_COMPOSE_DRIVE", None)
    env.pop("_COMPOSE_OLLAMA", None)
    env.pop("_COMPOSE_SEARXNG", None)
    channels = ask_channels(p, env)
    ask_tasks(p, env)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render(env))
    p.say(f"\nWrote {path} (readable by you only):")
    for line in summary(env):
        p.say(line)
    missing = [
        key
        for key, needed in (
            ("LLM_API_KEY", env["LLM_PROVIDER"] not in KEYLESS and "ENGINE" not in env),
            ("CLAUDE_CODE_OAUTH_TOKEN", "ENGINE" in env),
            ("TELEGRAM_CHAT_IDS", "TELEGRAM_BOT_TOKEN" in env),
            ("TS_AUTHKEY", "compose/tailscale.yml" in env.get("COMPOSE_FILE", "")),
            ("TUNNEL_TOKEN", "compose/cloudflared.yml" in env.get("COMPOSE_FILE", "")),
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
        if "compose/ollama.yml" in env.get("COMPOSE_FILE", ""):
            p.say(f"  Then: docker compose exec ollama ollama pull {env['LLM_MODEL']}")
        p.say(f"  The agent will answer at {address}")
        if "BROWSER_DOMAIN" in env:
            p.say("  Remote browser: scripts/browser-auth.sh user, before the first login.")
        if "DRIVES" in env:
            p.say("  Drives: log each one in before asking about your files (docs/drives.md).")
    else:
        extra = '".[subscription]"' if "ENGINE" in env else "."
        p.say(f"  Start: pip install -e {extra} && uvicorn intelligent_home_agent.main:app")
    p.say("  Check: docs/setup.md, step 6. Clients authenticate with AGENT_API_TOKEN from .env.")
    for channel in channels:
        p.say(f"  {NEXT_STEPS[channel]}")
    if "WHATSAPP_CHAT_ID" in env:
        p.say("  WhatsApp: link the number in WAHA's dashboard, then write in the chat")
        p.say("  (docs/chat.md).")
    if "TELEGRAM_BOT_TOKEN" in env:
        p.say("  Telegram: write to your bot once the agent runs (docs/chat.md).")
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
