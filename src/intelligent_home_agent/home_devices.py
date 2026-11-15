"""Home devices the agent drives directly, without Home Assistant (docs/home-devices.md).

Each one is on when its settings are filled in. Devices on the home network are reached
directly when the agent runs at home, or through ``HOME_PROXY_URL`` (a VPN sidecar's HTTP
proxy, ``HA_PROXY_URL`` by default) when it runs elsewhere.
"""

from __future__ import annotations

import httpx

from intelligent_home_agent.config import Settings
from intelligent_home_agent.tools.base import Tool
from intelligent_home_agent.tools.devialet import Devialet, build_devialet_tools
from intelligent_home_agent.tools.hue import Hue, build_hue_tools


def has_home_devices(settings: Settings) -> bool:
    return bool((settings.hue_bridge_url and settings.hue_app_key) or settings.devialet_url)


def home_network_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=settings.home_proxy_url or settings.ha_proxy_url,
        timeout=settings.home_timeout_seconds,
    )


def build_home_device_tools(settings: Settings, home: httpx.AsyncClient | None) -> list[Tool]:
    tools: list[Tool] = []
    if settings.hue_bridge_url and settings.hue_app_key:
        hue = Hue(home, settings.hue_bridge_url, settings.hue_app_key.get_secret_value())
        tools.extend(build_hue_tools(hue))
    if settings.devialet_url:
        speaker = Devialet(home, settings.devialet_url)
        tools.extend(build_devialet_tools(speaker, settings.devialet_max_volume))
    return tools
