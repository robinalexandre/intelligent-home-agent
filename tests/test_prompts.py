from intelligent_home_agent.activities import CATALOG
from intelligent_home_agent.prompts import PromptConfig, build_system_prompt

ALL_TOOLS = {
    "call_service",
    "get_weather",
    "get_calendar_events",
    "get_recent_emails",
    "get_travel_time",
    "get_github_pull_requests",
    "get_assistant_reminders",
    "get_wind_forecast",
    "get_surf_forecast",
    "get_outdoor_forecast",
    "create_reminder",
    "web_search",
    "remember",
    "forget",
    "find_procedure",
    "save_procedure",
    "turn_down_alert",
    "read_day_journal",
    "search_past_conversations",
}
SECTIONS = ["calendar", "travel", "mail", "pull_requests", "reminders", "weather", "activities"]


def prompt(**overrides):
    params = dict(
        assistant_name="Nova",
        language="en",
        timezone="UTC",
        tools=set(ALL_TOOLS),
        activities=[CATALOG["surf"], CATALOG["wingfoil"], CATALOG["cycling"]],
        brief_sections=list(SECTIONS),
        weather_detail="short",
        spot_example="North Beach",
    )
    params.update(overrides)
    return build_system_prompt(PromptConfig(**params))


def test_full_prompt_mentions_everything_configured():
    text = prompt()
    assert text.startswith("You are Nova")
    assert "Answer in English" in text
    assert "wingfoil: use get_wind_forecast" in text
    assert "cycling: use get_outdoor_forecast" in text
    assert '"Want the details for North Beach?"' in text
    assert "wave height and period" in text
    assert "first appointment, its time and when to leave" in text
    assert "get_outdoor_forecast for today and tomorrow" in text


def test_briefing_follows_the_enabled_sections():
    text = prompt(brief_sections=["calendar", "weather"], weather_detail="detailed")
    briefing = text.split("Morning briefing", 1)[1]
    assert "emails" not in briefing.split("Untrusted", 1)[0]
    assert "first appointment and its time" in briefing
    assert "wind and gusts if strong" in briefing
    assert "outdoor sessions" not in briefing


def test_weather_off_and_no_sources_means_no_briefing_paragraph():
    text = prompt(brief_sections=["weather"], weather_detail="off")
    assert "Morning briefing" not in text


def test_wind_activities_without_spots_fall_back_to_the_weather():
    tools = ALL_TOOLS - {"get_wind_forecast", "get_surf_forecast", "get_outdoor_forecast"}
    text = prompt(tools=tools, activities=[CATALOG["kitesurf"]])
    assert "kitesurf: no spots are configured; use get_weather" in text


def test_no_activities_and_no_tools_means_no_activity_paragraph():
    text = prompt(tools={"get_weather"}, activities=[])
    assert "Outdoor activities" not in text
    assert "Controlling the home" not in text
    assert "switched off" not in text


def test_french_prompt():
    text = prompt(language="fr")
    assert "Answer in French" in text
    assert '"le briefing"' in text
    assert "Tu veux le détail pour North Beach ?" in text
    assert "8h20" in build_system_prompt(
        PromptConfig("Nova", "fr", "UTC", tools={"get_weather"}), display="notification"
    )


def test_travel_time_needs_the_calendar():
    text = prompt(tools=ALL_TOOLS - {"get_calendar_events"})
    briefing = text.split("Morning briefing", 1)[1].split("Untrusted", 1)[0]
    assert "travel time" not in briefing
    assert "appointment" not in briefing


def test_recap_looks_at_tomorrow():
    recap = prompt().split("Evening recap", 1)[1]
    assert "get_outdoor_forecast for tomorrow" in recap
    reply = recap.split("- Reply", 1)[1]
    assert reply.index("what is still open today") < reply.index("tomorrow's first appointment")


def test_chat_display_memory_and_user_name():
    cfg = PromptConfig("Nova", "fr", "UTC", tools={"get_weather"}, user_name="Alex")
    text = build_system_prompt(cfg, display="chat", memory="- Likes tea.")
    assert "a message in a chat" in text and "read aloud" not in text
    assert '"Tu veux le détail ?"' in text and "8h20" in text
    assert "The user is called Alex" in text
    assert "What you remember about the user" in text and "- Likes tea." in text
    assert "What you remember" not in build_system_prompt(cfg)


def test_memory_tools_get_their_lines_only_when_registered():
    bare = prompt(tools={"get_weather"})
    for word in ("remember", "find_procedure", "turn_down_alert", "search_past_conversations"):
        assert word not in bare
    text = prompt(
        language="fr",
        tools={
            "remember",
            "find_procedure",
            "turn_down_alert",
            "search_past_conversations",
        },
    )
    assert 'when the user says "retiens que"' in text and "forget removes" in text
    assert "find_procedure first" in text and "save_procedure with its steps" in text
    assert "call turn_down_alert" in text
    assert "search_past_conversations before saying you don't remember" in text


def test_core_tools_get_their_paragraphs_only_when_registered():
    bare = prompt(tools={"get_weather"})
    assert "run_proposals" not in bare and "read_result_part" not in bare
    text = prompt(tools={"get_weather", "run_proposals", "read_result_part"})
    assert "Actions waiting for the user's go" in text
    assert '"yes", "oui"' in text and "drop_proposals" in text
    assert "read_result_part reads the next ones" in text
