import json

import pytest

from intelligent_home_agent import home_devices
from intelligent_home_agent.config import Settings
from intelligent_home_agent.home_devices import build_home_device_tools

BASE_ENV = {"AGENT_API_TOKEN": "t", "LLM_API_KEY": "k"}
CLOUD = {
    "NUKI_API_TOKEN": "n",
    "NETATMO_CLIENT_ID": "c",
    "NETATMO_CLIENT_SECRET": "s",
}
PETKIT = {"PETKIT_USERNAME": "pets@example.com", "PETKIT_PASSWORD": "p", "PETKIT_REGION": "GB"}
ANDROID_TV = {
    "ANDROID_TV_HOST": "192.168.1.60",
    "ANDROID_TV_CLIENT_CERT": "Q0VSVA==",
    "ANDROID_TV_CLIENT_KEY": "S0VZ",
}
APPLE = {
    "APPLE_DEVICES": json.dumps(
        [
            {
                "name": "Office",
                "host": "192.168.1.61",
                "id": "HP",
                "services": {
                    "AirPlay": {"port": 7000, "properties": {"model": "AudioAccessory5,1"}}
                },
            }
        ]
    )
}


def settings_of(monkeypatch, tmp_path, env):
    for key, value in {**BASE_ENV, "DATA_DIR": str(tmp_path), **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def names(monkeypatch, tmp_path, env, found=None):
    settings = settings_of(monkeypatch, tmp_path, env)
    return {t.name for t in build_home_device_tools(settings, found=found)}


def test_no_device_settings_no_tools(monkeypatch, tmp_path):
    assert names(monkeypatch, tmp_path, {}) == set()


def test_each_device_comes_with_its_settings(monkeypatch, tmp_path):
    found = names(
        monkeypatch,
        tmp_path,
        {
            "HUE_BRIDGE_URL": "http://192.168.1.50",
            "HUE_APP_KEY": "k",
            "DEVIALET_URL": "http://192.168.1.51",
            **CLOUD,
        },
    )
    assert found == {
        "list_hue_lights",
        "set_hue_lights",
        "activate_hue_scene",
        "get_speaker_state",
        "get_speaker_track",
        "control_speaker",
        "get_lock_state",
        "get_lock_activity",
        "lock_door",
        "unlock_door",
        "get_home_security_status",
        "get_camera_events",
        "get_netatmo_measures",
        "set_camera_monitoring",
    }


def test_a_hue_bridge_without_its_key_adds_nothing(monkeypatch, tmp_path):
    assert names(monkeypatch, tmp_path, {"HUE_BRIDGE_URL": "http://192.168.1.50"}) == set()


def test_unlocking_asks_for_a_code_once_one_is_set_up(monkeypatch, tmp_path):
    def unlock(env):
        settings = settings_of(monkeypatch, tmp_path, {"NUKI_API_TOKEN": "n", **env})
        return next(t for t in build_home_device_tools(settings) if t.name == "unlock_door")

    plain = unlock({})
    assert plain.always_propose and "code" not in plain.input_schema["properties"]
    coded = unlock({"UNLOCK_PHRASE": "a long enough secret phrase"})
    assert coded.never_park and "code" in coded.input_schema["properties"]


def test_petkit_takes_its_region_or_the_home_country(monkeypatch, tmp_path):
    pytest.importorskip("pypetkitapi")
    shared = {}
    found = names(monkeypatch, tmp_path, PETKIT, shared)
    assert found == {"get_pet_devices_status", "feed_pet_now", "clean_litter_box_now"}
    assert "petkit" in shared
    monkeypatch.delenv("PETKIT_REGION")
    assert names(monkeypatch, tmp_path, {}) == set()
    assert names(monkeypatch, tmp_path, {"HOME_COUNTRY": "GB"}) == found


def test_the_android_tv_device_takes_the_users_apps_and_channels(monkeypatch, tmp_path):
    pytest.importorskip("androidtvremote2")
    env = {
        **ANDROID_TV,
        "ANDROID_TV_NAME": "projector",
        "ANDROID_TV_APPS": '{"live tv": {"package": "com.example.livetv", "link": null}}',
        "ANDROID_TV_CHANNEL_APP": "live tv",
        "ANDROID_TV_CHANNELS": '{"News One": 1}',
    }
    settings = settings_of(monkeypatch, tmp_path, env)
    tools = {t.name: t for t in build_home_device_tools(settings)}
    assert set(tools) == {"get_tv_state", "control_tv"}
    control = tools["control_tv"]
    assert control.input_schema["properties"]["app"]["enum"][-1] == "live tv"
    assert "news one" in control.input_schema["properties"]["channel"]["description"]
    assert "the projector" in control.description


def test_apple_devices_come_from_their_profile(monkeypatch, tmp_path):
    pytest.importorskip("pyatv")
    assert names(monkeypatch, tmp_path, APPLE) == {"get_apple_media_state", "control_apple_media"}


def test_a_device_whose_library_is_missing_stays_off_and_says_so(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(home_devices.importlib.util, "find_spec", lambda name: None)
    with caplog.at_level("WARNING"):
        found = names(monkeypatch, tmp_path, {**PETKIT, **ANDROID_TV, **APPLE, **CLOUD})
    assert not found & {"get_pet_devices_status", "get_tv_state", "get_apple_media_state"}
    assert "get_lock_state" in found
    for extra in ("petkit", "android-tv", "apple"):
        assert f"intelligent-home-agent[{extra}]" in caplog.text
