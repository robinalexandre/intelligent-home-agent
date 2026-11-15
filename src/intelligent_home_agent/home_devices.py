"""Home devices the agent drives directly, without Home Assistant (docs/home-devices.md).

Each one is on when its settings are filled in. Devices on the home network are reached
directly when the agent runs at home, or through ``HOME_PROXY_URL`` (a VPN sidecar's HTTP
proxy, ``HA_PROXY_URL`` by default) when it runs elsewhere; cloud ones (Nuki, Netatmo,
PetKit, the car's maker) go out directly.

Three of them rest on unofficial libraries kept in optional extras (``petkit``,
``android-tv``, ``apple``): with the settings filled in but the extra missing, the device
stays off and the log says which extra to install.

The TV device's navigator (``ANDROID_TV_NAVIGATOR``, tools/tv_navigator.py) also needs the
``adb`` program and a model to drive it: the Claude subscription when there is one, the
provider's API otherwise. Without one of them it stays off and the log says which.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
from pathlib import Path
from typing import Any

import httpx

from intelligent_home_agent.config import Settings
from intelligent_home_agent.engine import NoApi
from intelligent_home_agent.engines import subscription_engine
from intelligent_home_agent.llm import LLMClient
from intelligent_home_agent.tools.base import Tool
from intelligent_home_agent.tools.car import CarAccount, build_car, build_car_tools
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
        '%s is set up but its library is missing: pip install ".[%s]" in the repository, '
        "or with Docker add %s to EXTRAS in .env and rebuild. It stays off.",
        what,
        extra,
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


def tv_navigator(settings: Settings, tv: Any, llm: LLMClient | None) -> Any:
    """The helper that drives the TV device's screen, or None with the reason logged: it
    needs the adb program, and a model (the subscription first, the API otherwise)."""
    from intelligent_home_agent.tools.adb import Adb
    from intelligent_home_agent.tools.tv_navigator import Navigator

    model = settings.android_tv_navigator_model
    engine = subscription_engine(settings, model)
    api = None if llm is None or isinstance(llm, NoApi) else llm
    off = "ANDROID_TV_NAVIGATOR is on but %s. navigate_tv stays off."
    if shutil.which("adb") is None:
        log.warning(off, "the adb program is not installed where the agent runs")
        return None
    if engine is None and api is None:
        log.warning(off, "no model can drive it: neither a Claude subscription nor an API")
        return None
    log.info(
        "navigate_tv: driven by %s (%s)",
        "the Claude subscription" + (", then the API" if api else "") if engine else "the API",
        model,
    )
    adb = Adb(
        settings.android_tv_host or "",
        str(Path(settings.data_dir) / "android"),
        settings.home_proxy_url or settings.ha_proxy_url,
    )
    return Navigator(
        tv,
        adb,
        model=model,
        # NoApi stays as the API when a subscription leads: its error says what is missing.
        llm=llm,
        engine=engine,
        max_actions=settings.android_tv_navigator_actions,
        language=settings.language,
    )


def build_home_device_tools(
    settings: Settings,
    home: httpx.AsyncClient | None = None,
    http: httpx.AsyncClient | None = None,
    found: dict[str, Any] | None = None,
    llm: LLMClient | None = None,
) -> list[Tool]:
    """home: the client for the home network; http: the one for cloud services; found:
    receives the clients others share (petkit: one session per account, so the supplies
    watch uses the tools' own) and those to close when the agent stops; llm: the model's
    API, for the TV device's navigator."""
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
        if settings.android_tv_navigator:
            from intelligent_home_agent.tools.tv_navigator import build_navigator_tools

            navigator = tv_navigator(settings, tv, llm)
            tools.extend(build_navigator_tools(navigator, settings.android_tv_name))
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
    # One proof for the door and the car: a code works once, whichever it opens.
    codes = unlock_codes(settings)
    if settings.nuki_api_token:
        nuki = Nuki(http, settings.nuki_api_token.get_secret_value())
        tools.extend(build_nuki_tools(nuki, settings.timezone, codes, language=language))
    if settings.car_enabled:
        account = CarAccount(
            vin=settings.car_vin or "",
            refresh_token=settings.car_refresh_token.get_secret_value(),
            client_id=settings.car_client_id,
            pin=settings.car_pin.get_secret_value() if settings.car_pin else None,
            # The maker may rotate the refresh token on each use: the current one.
            store=Path(settings.data_dir) / "car.json",
        )
        car = build_car(settings.car_provider, http, account)
        tools.extend(
            build_car_tools(car, settings.car_name, codes, settings.timezone, language=language)
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
