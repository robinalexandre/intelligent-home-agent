"""Home devices the agent drives directly, without Home Assistant (docs/home-devices.md).

Each one is on when its settings are filled in. Devices on the home network are reached
directly when the agent runs at home, or through ``HOME_PROXY_URL`` (a VPN sidecar's HTTP
proxy, ``HA_PROXY_URL`` by default) when it runs elsewhere; cloud ones (Nuki, Netatmo,
PetKit) go out directly.

Three of them rest on unofficial libraries kept in optional extras (``petkit``,
``android-tv``, ``apple``): with the settings filled in but the extra missing, the device
stays off and the log says which extra to install.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import httpx

from intelligent_home_agent.config import Settings
from intelligent_home_agent.tools.base import Tool
from intelligent_home_agent.tools.devialet import Devialet, build_devialet_tools
from intelligent_home_agent.tools.hue import Hue, build_hue_tools
from intelligent_home_agent.tools.netatmo import Netatmo, NetatmoAuth, build_netatmo_tools
from intelligent_home_agent.tools.nuki import Nuki, build_nuki_tools
from intelligent_home_agent.tools.unlock_codes import UnlockCodes

log = logging.getLogger(__name__)


def has_home_devices(settings: Settings) -> bool:
    """Whether a device spoken to over HTTP on the home network is set up."""
    return bool((settings.hue_bridge_url and settings.hue_app_key) or settings.devialet_url)


def home_network_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=settings.home_proxy_url or settings.ha_proxy_url,
        timeout=settings.home_timeout_seconds,
    )


def has_extra(module: str, extra: str, what: str) -> bool:
    """Whether an optional library is installed; says how to get it when it is not."""
    if importlib.util.find_spec(module) is not None:
        return True
    log.warning(
        '%s is set up but its library is missing: pip install "intelligent-home-agent[%s]". '
        "It stays off.",
        what,
        extra,
    )
    return False


def unlock_codes(settings: Settings) -> UnlockCodes | None:
    secret, phrase = settings.unlock_totp_secret, settings.unlock_phrase
    if not (secret or phrase):
        return None
    return UnlockCodes(
        Path(settings.data_dir) / "unlock-codes.json",
        secret.get_secret_value() if secret else None,
        phrase.get_secret_value() if phrase else None,
    )


def petkit_region(settings: Settings) -> str | None:
    return settings.petkit_region or settings.home_country


def build_home_device_tools(
    settings: Settings,
    home: httpx.AsyncClient | None = None,
    http: httpx.AsyncClient | None = None,
    found: dict[str, Any] | None = None,
) -> list[Tool]:
    """home: the client for the home network; http: the one for cloud services; found:
    receives the clients others share (petkit: one session per account, so the supplies
    watch uses the tools' own) and those to close when the agent stops."""
    tools: list[Tool] = []
    language = settings.language
    proxy = settings.home_proxy_url or settings.ha_proxy_url
    if settings.hue_bridge_url and settings.hue_app_key:
        hue = Hue(home, settings.hue_bridge_url, settings.hue_app_key.get_secret_value())
        tools.extend(build_hue_tools(hue))
    if settings.devialet_url:
        speaker = Devialet(home, settings.devialet_url)
        tools.extend(build_devialet_tools(speaker, settings.devialet_max_volume))
    if (
        settings.android_tv_host
        and settings.android_tv_client_cert
        and settings.android_tv_client_key
        and has_extra("androidtvremote2", "android-tv", "The Android TV device")
    ):
        from intelligent_home_agent.tools.android_tv import (
            AndroidTV,
            build_android_tv_tools,
            parse_apps,
            parse_channels,
        )

        tv = AndroidTV(
            settings.android_tv_host,
            settings.android_tv_client_cert.get_secret_value(),
            settings.android_tv_client_key.get_secret_value(),
            proxy,
            settings.home_timeout_seconds,
            apps=parse_apps(settings.android_tv_apps),
            channel_app=settings.android_tv_channel_app,
            channels=parse_channels(settings.android_tv_channels),
            name=settings.android_tv_name,
        )
        tools.extend(build_android_tv_tools(tv, settings.android_tv_max_volume))
        # Seam: reaching something inside an app (a film, an episode) needs a helper that
        # sees the screen (tools/adb.py) and runs its own model loop; not ported yet.
    if settings.apple_devices.strip() and has_extra("pyatv", "apple", "Apple TV and HomePods"):
        from intelligent_home_agent.tools.apple_media import (
            AppleMedia,
            build_apple_media_tools,
            parse_devices,
        )

        credentials = {
            "Companion": settings.apple_tv_companion_credentials,
            "AirPlay": settings.apple_tv_airplay_credentials,
        }
        apple = parse_devices(
            settings.apple_devices,
            {k: v.get_secret_value() for k, v in credentials.items() if v},
        )
        tools.extend(build_apple_media_tools(AppleMedia(apple, proxy)))
    if settings.nuki_api_token:
        nuki = Nuki(http, settings.nuki_api_token.get_secret_value())
        tools.extend(
            build_nuki_tools(nuki, settings.timezone, unlock_codes(settings), language=language)
        )
    if (
        settings.petkit_username
        and settings.petkit_password
        and petkit_region(settings)
        and has_extra("pypetkitapi", "petkit", "PetKit")
    ):
        from intelligent_home_agent.tools.petkit import PetKit, build_petkit_tools

        petkit = PetKit(
            settings.petkit_username,
            settings.petkit_password.get_secret_value(),
            petkit_region(settings) or "",
            settings.timezone,
        )
        tools.extend(build_petkit_tools(petkit, language=language))
        if found is not None:
            found["petkit"] = petkit
    if settings.netatmo_client_id and settings.netatmo_client_secret:
        token = settings.netatmo_refresh_token
        auth = NetatmoAuth(
            http,
            settings.netatmo_client_id,
            settings.netatmo_client_secret.get_secret_value(),
            token.get_secret_value() if token else None,
            Path(settings.data_dir) / "netatmo.json",
        )
        tools.extend(build_netatmo_tools(Netatmo(http, auth), settings.timezone, language=language))
    return tools
