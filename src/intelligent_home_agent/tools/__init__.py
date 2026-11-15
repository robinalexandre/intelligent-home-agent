from intelligent_home_agent.tools.apple_notes import ICloudNotes, build_note_tools
from intelligent_home_agent.tools.assistant_reminders import (
    ReminderRepo,
    build_assistant_reminder_tools,
)
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.browser import RemoteBrowser, build_browser_tools
from intelligent_home_agent.tools.calendar import (
    CalDavCalendar,
    build_calendar_tools,
    build_calendar_write_tools,
)
from intelligent_home_agent.tools.code_runner import build_code_tools
from intelligent_home_agent.tools.contacts import CardDavBirthdays, MergedCalendar
from intelligent_home_agent.tools.deliveries import Deliveries, build_delivery_tools
from intelligent_home_agent.tools.documents import build_document_tools
from intelligent_home_agent.tools.drive_trash import LocalTrash
from intelligent_home_agent.tools.drives import RcloneDrive, build_drive_tools, build_drives
from intelligent_home_agent.tools.find_my import FindMy, build_find_my_tools
from intelligent_home_agent.tools.general import build_general_tools
from intelligent_home_agent.tools.generation import Gemini, OpenAI, build_generation_tools
from intelligent_home_agent.tools.github import GitHub, build_github_tools
from intelligent_home_agent.tools.home_assistant import EntityPolicy, build_home_assistant_tools
from intelligent_home_agent.tools.illustrations import build_illustration_tools
from intelligent_home_agent.tools.knowledge import KnowledgeBase, build_knowledge_tools
from intelligent_home_agent.tools.long_results import build_long_result_tools
from intelligent_home_agent.tools.mail import ImapMailbox, build_draft_tools, build_email_tools
from intelligent_home_agent.tools.memory import GitHubFile, LocalFile, Memory, build_memory_tools
from intelligent_home_agent.tools.netatmo import Netatmo, NetatmoAuth, build_netatmo_tools
from intelligent_home_agent.tools.nuki import Nuki, build_nuki_tools
from intelligent_home_agent.tools.osint import Investigator, build_osint_tools
from intelligent_home_agent.tools.outdoor import build_outdoor_tools
from intelligent_home_agent.tools.past_conversations import build_past_conversation_tools
from intelligent_home_agent.tools.pdf_forms import build_pdf_form_tools
from intelligent_home_agent.tools.pdf_pages import build_pdf_page_tools
from intelligent_home_agent.tools.procedures import build_procedure_tools
from intelligent_home_agent.tools.proposals import build_proposal_tools
from intelligent_home_agent.tools.reminders import ICloudReminders, build_reminder_tools
from intelligent_home_agent.tools.surf import build_surf_tools
from intelligent_home_agent.tools.travel import Router, build_travel_tools
from intelligent_home_agent.tools.turned_down import TurnedDown, build_turned_down_tools
from intelligent_home_agent.tools.unlock_codes import UnlockCodes
from intelligent_home_agent.tools.weather import OpenMeteo, build_weather_tools, build_wind_tools
from intelligent_home_agent.tools.web import (
    build_engine_web_tools,
    build_search_engine,
    build_web_tools,
)
from intelligent_home_agent.tools.web_pages import (
    WebPages,
    build_web_page_tools,
    trusted_domains,
)
from intelligent_home_agent.tools.withings import Withings, WithingsAuth, build_withings_tools

__all__ = [
    "Deliveries",
    "Netatmo",
    "NetatmoAuth",
    "Nuki",
    "UnlockCodes",
    "Withings",
    "WithingsAuth",
    "build_delivery_tools",
    "build_netatmo_tools",
    "build_nuki_tools",
    "build_withings_tools",
    "CalDavCalendar",
    "CardDavBirthdays",
    "EntityPolicy",
    "Gemini",
    "FindMy",
    "GitHub",
    "GitHubFile",
    "ICloudNotes",
    "ICloudReminders",
    "ImapMailbox",
    "Investigator",
    "KnowledgeBase",
    "OpenAI",
    "LocalFile",
    "Memory",
    "MergedCalendar",
    "LocalTrash",
    "OpenMeteo",
    "RcloneDrive",
    "ReminderRepo",
    "RemoteBrowser",
    "Router",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "TurnedDown",
    "WebPages",
    "build_assistant_reminder_tools",
    "build_browser_tools",
    "build_calendar_tools",
    "build_code_tools",
    "build_document_tools",
    "build_calendar_write_tools",
    "build_draft_tools",
    "build_drive_tools",
    "build_drives",
    "build_email_tools",
    "build_engine_web_tools",
    "build_find_my_tools",
    "build_general_tools",
    "build_generation_tools",
    "build_github_tools",
    "build_home_assistant_tools",
    "build_illustration_tools",
    "build_knowledge_tools",
    "build_long_result_tools",
    "build_memory_tools",
    "build_note_tools",
    "build_osint_tools",
    "build_outdoor_tools",
    "build_pdf_form_tools",
    "build_pdf_page_tools",
    "build_past_conversation_tools",
    "build_procedure_tools",
    "build_proposal_tools",
    "build_reminder_tools",
    "build_search_engine",
    "build_surf_tools",
    "build_travel_tools",
    "build_turned_down_tools",
    "build_weather_tools",
    "build_web_page_tools",
    "build_web_tools",
    "build_wind_tools",
    "trusted_domains",
]

# Notes for the user's coding sessions, their repositories, and the agent's own tasks.
from intelligent_home_agent.tools.assistant_notes import (  # noqa: E402
    LocalFolder,
    NotesRepo,
    RepoFolder,
    build_assistant_note_tools,
)
from intelligent_home_agent.tools.repos import Repositories, build_repository_tools  # noqa: E402
from intelligent_home_agent.tools.scheduled_tasks import build_task_tools  # noqa: E402
from intelligent_home_agent.tools.vulnerabilities import (  # noqa: E402
    Vulnerabilities,
    build_vulnerability_tools,
)

__all__ += [
    "LocalFolder",
    "NotesRepo",
    "RepoFolder",
    "Repositories",
    "Vulnerabilities",
    "build_assistant_note_tools",
    "build_repository_tools",
    "build_task_tools",
    "build_vulnerability_tools",
]
