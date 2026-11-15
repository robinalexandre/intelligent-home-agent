from intelligent_home_agent.activities import CATALOG
from intelligent_home_agent.prompts import PromptConfig, build_system_prompt

ALL_TOOLS = {
    "call_service",
    "get_weather",
    "get_calendar_events",
    "get_recent_emails",
    "create_calendar_event",
    "search_emails",
    "read_email",
    "save_email_draft",
    "flag_email",
    "move_email",
    "list_email_folders",
    "create_email_folder",
    "list_email_drafts",
    "update_email_draft",
    "delete_email_draft",
    "get_travel_time",
    "get_github_pull_requests",
    "get_assistant_reminders",
    "get_wind_forecast",
    "get_surf_forecast",
    "get_outdoor_forecast",
    "create_reminder",
    "web_search",
    "list_saved_files",
    "read_saved_file",
    "create_pdf",
    "read_pdf_layout",
    "fill_pdf",
    "copy_pdf_pages",
    "draw_image",
    "make_animation",
    "find_exercise_photos",
    "save_exercise_photos",
    "generate_image",
    "generate_video",
    "run_code",
    "remember",
    "forget",
    "find_procedure",
    "save_procedure",
    "turn_down_alert",
    "read_day_journal",
    "search_past_conversations",
    "open_web_page",
    "inspect_file",
    "domain_lookup",
    "osm_lookup",
    "reverse_image_search",
    "list_drive_folder",
    "search_drive_files",
    "search_drive_content",
    "read_drive_file",
    "create_drive_folder",
    "save_file_to_drive",
    "delete_drive_file",
    "control_tv",
    "control_apple_media",
    "lock_door",
    "get_home_security_status",
    "get_pet_devices_status",
    "get_body_measurements",
    "note_delivery",
    "list_deliveries",
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


def test_pages_and_investigations_are_only_mentioned_when_there():
    text = prompt()
    assert "open_web_page reads a public page" in text and "web_search or a site" in text
    assert "work like an investigator" in text and "reverse_image_search puts it" in text
    assert "putting a name on an unknown private person" in text
    text = prompt(tools=ALL_TOOLS - {"reverse_image_search", "web_search"})
    assert "reverse_image_search" not in text and "web_search" not in text
    assert "open_web_page and the archives" in text
    text = prompt(tools={"get_weather", "web_search"})
    assert "open_web_page" not in text and "investigator" not in text


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


def test_documents_and_pictures_are_mentioned_only_when_their_tools_exist():
    text = prompt()
    for line in (
        "create_pdf writes a PDF",
        "draw it with draw_image",
        "find_exercise_photos has",
        "Generated pictures (generate_image / generate_video)",
        "read_pdf_layout, then fill_pdf",
        "copy_pdf_pages makes a new PDF",
        "run_code computes exactly",
        "look at list_saved_files",
    ):
        assert line in text
    # Files are others' words: named once among what closes the request to writes.
    assert text.count("kept files were read") == 1

    bare = prompt(tools={"get_weather", "draw_image", "generate_image"})
    assert "draw it with draw_image" in bare and "find_exercise_photos" not in bare
    assert "Generated pictures (generate_image)" in bare
    for absent in ("create_pdf", "fill_pdf", "copy_pdf_pages", "run_code", "list_saved_files"):
        assert absent not in bare
    assert "kept files were read" not in bare


def test_drives_are_described_by_what_is_on():
    full = prompt()
    assert "search_drive_content finds them by what they say" in full
    assert "Gathering files is always a copy" in full and "delete_drive_without_copy" in full
    assert "files of a drive, kept files were read" in full
    read_only = prompt(tools={"list_drive_folder", "search_drive_files", "read_drive_file"})
    assert "The drives are read-only" in read_only
    assert "search_drive_content" not in read_only and "create_drive_folder" not in read_only
    assert "drive" not in prompt(tools={"get_weather"}).split("Untrusted")[0]


def test_filing_from_emails_into_a_drive_needs_both():
    both = ALL_TOOLS | {"read_email", "flag_email", "save_email_draft"}
    text = prompt(tools=both)
    assert "Filing from emails" in text and "and flag_email, so the user sees" in text
    assert "never write one yourself with create_pdf" in text
    assert "kept from an email or read from a drive" in text
    assert "Filing from emails" not in prompt(tools=ALL_TOOLS - {"read_email"})
    plain = prompt(tools={"list_drive_folder", "create_drive_folder", "read_email"})
    assert "Filing from emails" in plain and "flag_email" not in plain
    assert "create_pdf" not in plain
