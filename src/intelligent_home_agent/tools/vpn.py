"""The VPN the remote browser goes abroad through: a gluetun container, which speaks to
most VPN providers (compose/vpn.yml takes the provider as a setting).

gluetun connects to one country at a time and serves an HTTP proxy that the browser's
filtering proxy (compose/browser/squid-vpn.conf, its "abroad" port) hands the abroad pages
to. Its control server, behind an API key, switches the country and stops the tunnel: the
VPN runs only while a request browses abroad, and stops right after (tools/browser.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

from intelligent_home_agent.tools.base import ToolError

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 60
POLL_SECONDS = 1.0
COUNTRY_NAME = re.compile(r"[A-Za-z][A-Za-z .'-]{2,40}")

# Language the abroad browser asks pages in, by country (gluetun's names); others: English.
LOCALES = {
    "argentina": "es-AR", "australia": "en-AU", "austria": "de-AT", "belgium": "fr-BE",
    "brazil": "pt-BR", "bulgaria": "bg-BG", "canada": "en-CA", "chile": "es-CL",
    "colombia": "es-CO", "croatia": "hr-HR", "czech republic": "cs-CZ", "denmark": "da-DK",
    "egypt": "ar-EG", "estonia": "et-EE", "finland": "fi-FI", "france": "fr-FR",
    "germany": "de-DE", "greece": "el-GR", "hong kong": "zh-HK", "hungary": "hu-HU",
    "iceland": "is-IS", "india": "en-IN", "indonesia": "id-ID", "ireland": "en-IE",
    "israel": "he-IL", "italy": "it-IT", "japan": "ja-JP", "malaysia": "ms-MY",
    "mexico": "es-MX", "morocco": "fr-MA", "netherlands": "nl-NL", "new zealand": "en-NZ",
    "nigeria": "en-NG", "norway": "nb-NO", "philippines": "en-PH", "poland": "pl-PL",
    "portugal": "pt-PT", "romania": "ro-RO", "serbia": "sr-RS", "singapore": "en-SG",
    "slovakia": "sk-SK", "south africa": "en-ZA", "south korea": "ko-KR", "spain": "es-ES",
    "sweden": "sv-SE", "switzerland": "de-CH", "taiwan": "zh-TW", "thailand": "th-TH",
    "turkey": "tr-TR", "ukraine": "uk-UA", "united arab emirates": "ar-AE",
    "united kingdom": "en-GB", "united states": "en-US", "vietnam": "vi-VN",
}  # fmt: skip


@dataclass(frozen=True)
class Exit:
    """Where the tunnel came out, as the internet sees it."""

    country: str
    city: str
    timezone: str
    locale: str


def country_name(value: str) -> str:
    value = " ".join(str(value or "").split())
    if not COUNTRY_NAME.fullmatch(value):
        raise ToolError("Give the country by its English name, e.g. Spain or United States.")
    return value


def _same_country(asked: str, seen: str) -> bool:
    def norm(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z]", "", text.lower())

    return bool(seen) and norm(asked) == norm(seen)


class Vpn:
    def __init__(self, http: httpx.AsyncClient, control_url: str, api_key: str) -> None:
        self._http = http
        self._url = control_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}

    async def _call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        try:
            response = await self._http.request(
                method, f"{self._url}{path}", json=body, headers=self._headers, timeout=15
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"The VPN container doesn't answer: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise ToolError("The VPN refused the agent's key (VPN_CONTROL_KEY).")
        return response

    async def running(self) -> bool:
        response = await self._call("GET", "/v1/vpn/status")
        return response.is_success and response.json().get("status") == "running"

    async def connect(self, country: str) -> Exit:
        """Bring the tunnel up in that country, and wait until the internet sees it there."""
        response = await self._call(
            "PUT", "/v1/vpn/settings", {"provider": {"server_selection": {"countries": [country]}}}
        )
        if not response.is_success:
            raise ToolError(
                f"The VPN provider can't go to {country} (it may not serve it, or name it "
                f"otherwise): {response.text.strip()[:300]}"
            )
        response = await self._call("PUT", "/v1/vpn/status", {"status": "running"})
        if not response.is_success:
            raise ToolError(f"The VPN didn't start: {response.text.strip()[:300]}")
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        seen: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = await self._call("GET", "/v1/publicip/ip")
            seen = response.json() if response.is_success else {}
            if seen.get("public_ip") and _same_country(country, seen.get("country") or ""):
                return Exit(
                    country=seen["country"],
                    city=seen.get("city") or "",
                    timezone=seen.get("timezone") or "",
                    locale=LOCALES.get(country.lower(), "en-US"),
                )
            await asyncio.sleep(POLL_SECONDS)
        await self.disconnect()
        where = seen.get("country") or "nowhere yet"
        raise ToolError(f"The VPN didn't come out in {country} ({where}): it is stopped again.")

    async def disconnect(self) -> None:
        response = await self._call("PUT", "/v1/vpn/status", {"status": "stopped"})
        if not response.is_success:
            raise ToolError(f"The VPN didn't stop: {response.text.strip()[:300]}")
        log.info("VPN stopped")
