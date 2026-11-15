from intelligent_home_agent.tools.assistant_reminders import (
    ReminderRepo,
    build_assistant_reminder_tools,
)
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.calendar import CalDavCalendar, build_calendar_tools
from intelligent_home_agent.tools.code_runner import build_code_tools
from intelligent_home_agent.tools.documents import build_document_tools
from intelligent_home_agent.tools.general import build_general_tools
from intelligent_home_agent.tools.generation import Gemini, OpenAI, build_generation_tools
from intelligent_home_agent.tools.github import GitHub, build_github_tools
from intelligent_home_agent.tools.home_assistant import EntityPolicy, build_home_assistant_tools
from intelligent_home_agent.tools.illustrations import build_illustration_tools
from intelligent_home_agent.tools.knowledge import KnowledgeBase, build_knowledge_tools
from intelligent_home_agent.tools.long_results import build_long_result_tools
from intelligent_home_agent.tools.mail import ImapMailbox, build_email_tools
from intelligent_home_agent.tools.outdoor import build_outdoor_tools
from intelligent_home_agent.tools.pdf_forms import build_pdf_form_tools
from intelligent_home_agent.tools.pdf_pages import build_pdf_page_tools
from intelligent_home_agent.tools.proposals import build_proposal_tools
from intelligent_home_agent.tools.surf import build_surf_tools
from intelligent_home_agent.tools.travel import Router, build_travel_tools
from intelligent_home_agent.tools.weather import OpenMeteo, build_weather_tools, build_wind_tools
from intelligent_home_agent.tools.web import (
    build_engine_web_tools,
    build_search_engine,
    build_web_tools,
)

__all__ = [
    "CalDavCalendar",
    "EntityPolicy",
    "Gemini",
    "GitHub",
    "ImapMailbox",
    "KnowledgeBase",
    "OpenAI",
    "OpenMeteo",
    "ReminderRepo",
    "Router",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "build_assistant_reminder_tools",
    "build_calendar_tools",
    "build_code_tools",
    "build_document_tools",
    "build_email_tools",
    "build_engine_web_tools",
    "build_general_tools",
    "build_generation_tools",
    "build_github_tools",
    "build_home_assistant_tools",
    "build_illustration_tools",
    "build_knowledge_tools",
    "build_long_result_tools",
    "build_outdoor_tools",
    "build_pdf_form_tools",
    "build_pdf_page_tools",
    "build_proposal_tools",
    "build_search_engine",
    "build_surf_tools",
    "build_travel_tools",
    "build_weather_tools",
    "build_web_tools",
    "build_wind_tools",
]
