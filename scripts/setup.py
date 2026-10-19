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


def ask_model(p: Prompter, env: dict[str, str]) -> None:
    p.say("\n# Language model (Anthropic is the provider shipped; see AGENTS.md to add one)")
    env["LLM_PROVIDER"] = "anthropic"
    if key := p.secret("API key"):
        env["LLM_API_KEY"] = key
    env["LLM_MODEL"] = p.ask("Model", "claude-sonnet-5-5")


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
                repo = p.ask("Repo holding coding-assistant reminders, owner/repo (empty: none)")
                if repo:
                    env["ASSISTANT_REMINDERS_REPO"] = repo


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
    ("Language model", ["LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL"]),
    ("Assistant", ["ASSISTANT_NAME", "LANGUAGE", "TIMEZONE"]),
    ("Home", ["HOME_CITY", "HOME_COUNTRY", "HOME_LATITUDE", "HOME_LONGITUDE"]),
    ("Briefing", ["BRIEF_SECTIONS", "WEATHER_DETAIL"]),
    ("Outdoor activities", ["OUTDOOR_ACTIVITIES", "WIND_SPOTS", "SURF_SPOTS"]),
    ("Home Assistant", ["HA_URL", "HA_TOKEN"]),
    ("Calendar", ["CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"]),
    ("Mail", ["MAIL_ACCOUNTS"]),
    ("GitHub", ["GITHUB_TOKEN", "ASSISTANT_REMINDERS_REPO"]),
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
    ask_identity(p, env)
    ask_model(p, env)
    ask_home(p, env, lookup)
    sections = ask_brief(p, env)
    ask_activities(p, env)
    ask_sources(p, env, sections)
    channels = ask_channels(p, env)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(render(env))
    p.say(f"\nWrote {path} (readable by you only):")
    for line in summary(env):
        p.say(line)
    if "LLM_API_KEY" not in env:
        p.say("\nLLM_API_KEY is still empty: add it to .env before starting.")
    p.say("\nNext steps:")
    p.say("  Start: docker compose up -d --build   (or: uvicorn intelligent_home_agent.main:app)")
    p.say("  Clients authenticate with AGENT_API_TOKEN from .env.")
    for channel in channels:
        p.say(f"  {NEXT_STEPS[channel]}")
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
