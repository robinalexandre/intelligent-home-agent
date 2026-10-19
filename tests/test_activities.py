import pytest

from intelligent_home_agent.activities import CATALOG, forecasts_for, match_activities, resolve


@pytest.mark.parametrize(
    "text, expected",
    [
        ("surf, wing and some cycling", ["surf", "wingfoil", "cycling"]),
        ("Je fais du vélo et de la randonnée", ["cycling", "hiking"]),
        ("kitesurfing / ski", ["kitesurf", "skiing"]),
        ("I go out a lot", ["outdoors"]),
        ("golf", ["golf"]),
    ],
)
def test_free_text_matches_activities(text, expected):
    found, unknown = match_activities(text)
    assert found == expected
    assert unknown == []


def test_unknown_fragments_are_reported():
    found, unknown = match_activities("running and underwater hockey")
    assert found == ["running"]
    assert unknown == ["underwater hockey"]


def test_resolve_accepts_aliases_and_rejects_unknown_names():
    assert [a.key for a in resolve(["wing", "bike", "wingfoil"])] == ["wingfoil", "cycling"]
    with pytest.raises(ValueError, match="chess"):
        resolve(["chess"])


def test_forecasts_are_merged_in_order():
    assert forecasts_for([CATALOG["surf"], CATALOG["cycling"]]) == [
        "waves",
        "tides",
        "wind",
        "rain",
        "temperature",
    ]


def test_every_weather_activity_has_at_least_one_limit():
    for activity in CATALOG.values():
        if activity.kind == "weather":
            limits = (
                activity.max_rain_pct,
                activity.max_wind_kmh,
                activity.min_temp_c,
                activity.max_temp_c,
                activity.min_visibility_m,
            )
            assert any(v is not None for v in limits), activity.key
