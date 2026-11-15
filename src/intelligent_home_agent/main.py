"""HTTP API: what the Siri Shortcut, Home Assistant or any other client talks to."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import secrets
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

from intelligent_home_agent import __version__, curation
from intelligent_home_agent.activities import CATALOG
from intelligent_home_agent.activity_alerts import (
    ActivityAlerts,
    Conditions,
    build_activity_alert_tools,
)
from intelligent_home_agent.agent import REPEATING_TAG, TASK_TAG, Agent
from intelligent_home_agent.chat import ChatChannel
from intelligent_home_agent.chat_session import (
    PLAN_TAG,
    ChatReports,
    ChatSession,
    too_long_for_option,
)
from intelligent_home_agent.coder_approvals import Approvals, build_coder_permission_tools
from intelligent_home_agent.config import Place, Settings, get_settings, home_on
from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError, patterns_from_attributes
from intelligent_home_agent.config_crypt import available as crypt_available
from intelligent_home_agent.conversations import (
    Attachment,
    Conversation,
    ConversationStore,
    FileStore,
)
from intelligent_home_agent.deliver import Delivery, NtfyReports, Outbox, ReportChannel
from intelligent_home_agent.departure import Departure
from intelligent_home_agent.departure import record as record_departure
from intelligent_home_agent.drive_content import (
    build_drive_content_tools,
    build_drive_contents,
    model_ocr,
)
from intelligent_home_agent.engine import NoApi, SubscriptionFirst
from intelligent_home_agent.engines import check_subscription, subscription_engine
from intelligent_home_agent.ha_client import HomeAssistantClient
from intelligent_home_agent.home_devices import (
    build_home_device_tools,
    has_home_devices,
    home_network_client,
)
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.icloud_web import ICloudWeb, session_dir
from intelligent_home_agent.icloud_web import available as icloud_available
from intelligent_home_agent.jobs import DONE as JOB_DONE
from intelligent_home_agent.jobs import FAILED as JOB_FAILED
from intelligent_home_agent.jobs import IDLE as JOB_IDLE
from intelligent_home_agent.jobs import STOPPED as JOB_STOPPED
from intelligent_home_agent.jobs import Backend, Job, JobRunner, JobStore
from intelligent_home_agent.jobs_claude_code import ClaudeCodeBackend
from intelligent_home_agent.journal import Journal
from intelligent_home_agent.llm import ANTHROPIC, LLMClient, LLMError, build_llm_client
from intelligent_home_agent.notify import NotifyError, Ntfy
from intelligent_home_agent.pet_alerts import PetAlerts
from intelligent_home_agent.progress import Tracker
from intelligent_home_agent.prompts import PromptConfig
from intelligent_home_agent.review import NightlyReview, lessons, review_note, reviewed_day
from intelligent_home_agent.shared_links import LINKS
from intelligent_home_agent.tasks import (
    TaskReports,
    TaskRunner,
    built_in_tasks,
    says_nothing,
)
from intelligent_home_agent.telegram import TelegramApi, TelegramBot
from intelligent_home_agent.tools import (
    CalDavCalendar,
    CardDavBirthdays,
    EntityPolicy,
    FindMy,
    GitHub,
    GitHubFile,
    ICloudNotes,
    ICloudReminders,
    ImapMailbox,
    Investigator,
    KnowledgeBase,
    LocalFile,
    LocalTrash,
    Memory,
    MergedCalendar,
    OpenMeteo,
    ReminderRepo,
    RemoteBrowser,
    Router,
    Tool,
    ToolRegistry,
    TurnedDown,
    WebPages,
    build_assistant_reminder_tools,
    build_browser_tools,
    build_calendar_tools,
    build_calendar_write_tools,
    build_draft_tools,
    build_drive_tools,
    build_drives,
    build_email_tools,
    build_engine_web_tools,
    build_find_my_tools,
    build_general_tools,
    build_github_tools,
    build_home_assistant_tools,
    build_knowledge_tools,
    build_live_sources,
    build_live_wind_tools,
    build_memory_tools,
    build_note_tools,
    build_osint_tools,
    build_outdoor_tools,
    build_past_conversation_tools,
    build_procedure_tools,
    build_reminder_tools,
    build_search_engine,
    build_surf_tools,
    build_travel_tools,
    build_turned_down_tools,
    build_weather_tools,
    build_web_page_tools,
    build_web_tools,
    build_wind_tools,
    live_wind_reporter,
    trusted_domains,
)
from intelligent_home_agent.tools.assistant_notes import (
    LocalFolder,
    NoteFolder,
    NotesRepo,
    RepoFolder,
    build_assistant_note_tools,
)
from intelligent_home_agent.tools.browser import available as browser_available
from intelligent_home_agent.tools.code_runner import build_code_tools
from intelligent_home_agent.tools.connectors import Connector, build_connector_tools
from intelligent_home_agent.tools.deliveries import Deliveries, build_delivery_tools
from intelligent_home_agent.tools.documents import FONT_DIR, build_document_tools, paper_for
from intelligent_home_agent.tools.generation import (
    OPENAI_URL,
    Gemini,
    OpenAI,
    Provider,
    build_generation_tools,
)
from intelligent_home_agent.tools.github import API_URL
from intelligent_home_agent.tools.hosting import build_hosting_provider, build_hosting_tools
from intelligent_home_agent.tools.illustrations import build_illustration_tools
from intelligent_home_agent.tools.jobs import build_job_tools
from intelligent_home_agent.tools.long_results import build_long_result_tools
from intelligent_home_agent.tools.mcp_client import (
    MCPClient,
    NoAuth,
    OAuthToken,
    StaticToken,
    TokenSource,
)
from intelligent_home_agent.tools.memory import folder_name
from intelligent_home_agent.tools.pdf_forms import build_pdf_form_tools
from intelligent_home_agent.tools.pdf_pages import build_pdf_page_tools
from intelligent_home_agent.tools.proposals import build_proposal_tools
from intelligent_home_agent.tools.repos import Repositories, build_repository_tools
from intelligent_home_agent.tools.scheduled_tasks import build_task_tools
from intelligent_home_agent.tools.vpn import Vpn
from intelligent_home_agent.tools.vulnerabilities import (
    Vulnerabilities,
    build_vulnerability_tools,
)
from intelligent_home_agent.tools.whatsapp_chats import build_whatsapp_chat_tools
from intelligent_home_agent.tools.withings import Withings, WithingsAuth, build_withings_tools
from intelligent_home_agent.transcription import Transcriber, shared_transcriber
from intelligent_home_agent.transcription import available as voice_available
from intelligent_home_agent.whatsapp import Waha, WhatsAppChannel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# A chat channel asks for news every few seconds, and a Telegram address holds the bot's
# token: neither belongs in the log. Tool calls are logged anyway.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

DEFAULT_CONVERSATION = "default"
MAX_FILES = 5
# A PDF of 20 MB, the most the agent reads, once in base64.
MAX_FILE_BASE64_CHARS = 28_000_000


class FileIn(BaseModel):
    """A file sent with a request: read in that request, then kept by name."""

    filename: str = Field(min_length=1, max_length=200)
    # e.g. application/pdf, image/jpeg, text/plain, or an office document's type.
    mimetype: str = Field(min_length=1, max_length=200)
    # The file's bytes, in base64.
    data: str = Field(min_length=1, max_length=MAX_FILE_BASE64_CHARS)

    @field_validator("data")
    @classmethod
    def _is_base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data must be the file in base64") from exc
        return value

    def attachment(self) -> Attachment:
        return Attachment(self.filename, self.mimetype, base64.b64decode(self.data))


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: str = Field(default=DEFAULT_CONVERSATION, max_length=64)
    # Extra data from the phone, e.g. today's reminders for the morning briefing.
    context: str | None = Field(default=None, max_length=4000)
    # "notification" when the reply is read on screen (briefing, recap), not spoken;
    # "chat" when it is sent in a chat the user can answer.
    display: Literal["voice", "notification", "chat"] = "voice"
    # Files the user sends along (a PDF, a photo, a text or office document).
    files: list[FileIn] = Field(default=[], max_length=MAX_FILES)
    # "chat" also sends the reply in the chat channel (docs/chat.md) and continues the
    # conversation there, whatever conversation_id and display say; a channel's name
    # ("whatsapp", "telegram") picks one when several are set up.
    deliver: str = Field(default="response", max_length=64)


class FileOut(BaseModel):
    # GET /v1/files/{name}?conversation_id=... downloads it while the conversation lasts.
    name: str
    mimetype: str


class AskResponse(BaseModel):
    reply: str
    conversation_id: str
    # Things the client must do on the phone, e.g. {"type": "reminder", "title": ...}.
    actions: list[dict] = []
    # Files the agent made or fetched for the user in this request.
    files: list[FileOut] | None = None
    # The Shortcut listens again unless the user just closed the conversation. Left out
    # rather than false, since a missing key is what Shortcuts tests reliably.
    follow_up: bool | None = None


class BriefingRequest(BaseModel):
    kind: Literal["morning", "evening"] = "morning"
    # Same as AskRequest.context: e.g. open reminders sent by the phone.
    context: str | None = Field(default=None, max_length=4000)
    # Also send the reply as a push notification (needs NTFY_URL).
    push: bool = False
    # "chat" (or a channel's name) sends the briefing in the chat channel, where the
    # user can answer it: it then joins that chat's conversation (docs/chat.md).
    deliver: str = Field(default="response", max_length=64)


class BriefingResponse(BaseModel):
    reply: str
    actions: list[dict] = []
    pushed: bool = False
    # Sent in the chat channel (deliver).
    delivered: bool = False


class DepartureRequest(BaseModel):
    # The conversation the user answers in: their yes there runs the fixes.
    conversation_id: str = Field(default=DEFAULT_CONVERSATION, max_length=64)
    # Also send the message as a push notification (needs NTFY_URL).
    push: bool = False
    # "chat" (or a channel's name) says it in the chat channel instead, where each fix is
    # a tick away (docs/chat.md); conversation_id is then that chat's.
    deliver: str = Field(default="response", max_length=64)


class DepartureResponse(BaseModel):
    # Empty when everything is locked and off: say nothing then.
    reply: str
    conversation_id: str
    pushed: bool = False
    # Fixes wait for the user's yes in this conversation.
    follow_up: bool | None = None


def closes_conversation(text: str, language: str = "en", assistant_name: str = "") -> bool:
    """True when the message only says thanks or goodbye ("no thanks", "that's all")."""
    closing = strings(language).closing_words | strings("en").closing_words
    closing |= set(assistant_name.casefold().split())
    words = re.findall(r"[\w']+", text.casefold().replace("’", "'"))
    return bool(words) and all(word in closing for word in words)


def build_registry(
    settings: Settings,
    ha: HomeAssistantClient | None,
    http: httpx.AsyncClient,
    llm: LLMClient,
    home: httpx.AsyncClient | None = None,
    kept: Kept | None = None,
) -> ToolRegistry:
    policy = EntityPolicy.from_lists(
        settings.ha_exposed_domains, settings.ha_sensitive_domains, settings.ha_entity_denylist
    )
    meteo = OpenMeteo(http, settings.timezone)
    # The client for the home network; `home` is the home's coordinates from here on.
    home_network = home
    first_home = None
    if settings.home_latitude is not None and settings.home_longitude is not None:
        first_home = (settings.home_latitude, settings.home_longitude)

    def home_today() -> Place | None:
        # Home may move (HOME_MOVES): forecasts and alerts ask where it is each time.
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        return home_on(first_home, settings.home_moves, today)

    home = home_today()

    registry = ToolRegistry()
    icloud = build_icloud(settings)
    # With iCloud, reminders are written there; else the client app makes them on
    # the phone.
    registry.extend(build_general_tools(settings.timezone, phone_reminders=icloud is None))
    if icloud is not None:
        reminders = ICloudReminders(
            icloud, settings.icloud_reminder_lists, settings.icloud_reminders_default_list
        )
        registry.extend(build_reminder_tools(reminders, settings.timezone, settings.language))
        if settings.icloud_notes:
            registry.extend(build_note_tools(ICloudNotes(icloud), settings.timezone))
        if settings.icloud_find_my:
            registry.extend(
                build_find_my_tools(FindMy(icloud), settings.timezone, home, settings.language)
            )
    if ha is not None:
        registry.extend(build_home_assistant_tools(ha, policy))
    # The clients others share (the pet supplies watch) and those to close at the end.
    registry.devices = {}
    registry.extend(build_home_device_tools(settings, home_network, http, registry.devices, llm))
    if settings.withings_client_id and settings.withings_client_secret:
        token = settings.withings_refresh_token
        auth = WithingsAuth(
            http,
            settings.withings_client_id,
            settings.withings_client_secret.get_secret_value(),
            token.get_secret_value() if token else None,
            Path(settings.data_dir) / "withings.json",
        )
        registry.extend(build_withings_tools(Withings(http, auth), settings.timezone))
    registry.extend(build_weather_tools(meteo, home=home_today, language=settings.language))
    registry.extend(build_wind_tools(meteo, settings.wind_spots, model=settings.wind_model))
    registry.extend(build_surf_tools(meteo, settings.surf_spots, model=settings.wind_model))
    activities = [CATALOG[key] for key in settings.outdoor_activities]
    registry.extend(
        build_outdoor_tools(meteo, activities, home=home_today, language=settings.language)
    )
    # Wind measured now, from the sources named in LIVE_WIND_SOURCES (docs/outdoor.md).
    mf_id = settings.meteofrance_application_id
    live_sources = build_live_sources(
        http,
        settings.live_wind_sources,
        meteofrance_application_id=mf_id.get_secret_value() if mf_id else None,
    )
    spots = [*settings.wind_spots, *settings.surf_spots]
    registry.extend(
        build_live_wind_tools(
            spots,
            live_sources,
            timezone=settings.timezone,
            radius_km=settings.live_wind_radius_km,
            home=home_today,
        )
    )
    # Activity alerts: only when something can carry them to the user.
    registry.activity_alerts = None
    if alerts_reach(settings):
        live = None
        if live_sources:
            live = live_wind_reporter(
                live_sources, timezone=settings.timezone, radius_km=settings.live_wind_radius_km
            )
        registry.activity_alerts = ActivityAlerts(
            Path(settings.data_dir) / "activity-alerts.json",
            Conditions(
                meteo,
                wind_spots=settings.wind_spots,
                surf_spots=settings.surf_spots,
                model=settings.wind_model,
                home=home_today,
                live=live,
            ),
            settings.timezone,
            language=settings.language,
            lead_hours=settings.activity_alerts_lead_hours,
            quiet=(settings.activity_alerts_quiet_start, settings.activity_alerts_quiet_end),
        )
        registry.activity_alerts.seed(settings.activity_alerts)
        registry.extend(
            build_activity_alert_tools(registry.activity_alerts, language=settings.language)
        )
    calendars: list = []
    if settings.caldav_url and settings.caldav_username and settings.caldav_password:
        calendar = CalDavCalendar(
            settings.caldav_url,
            settings.caldav_username,
            settings.caldav_password.get_secret_value(),
            settings.caldav_calendars,
            settings.caldav_default_calendar,
        )
        calendars.append(calendar)
        if settings.caldav_write:
            registry.extend(
                build_calendar_write_tools(calendar, settings.timezone, settings.language)
            )
    # Contacts' birthdays show as all-day events; the CalDAV login is used when CardDAV
    # has none of its own (same account at most providers).
    dav_user = settings.carddav_username or settings.caldav_username
    dav_password = settings.carddav_password or settings.caldav_password
    if settings.carddav_url and dav_user and dav_password:
        calendars.append(
            CardDavBirthdays(
                http,
                settings.carddav_url,
                dav_user,
                dav_password.get_secret_value(),
                settings.timezone,
            )
        )
    if calendars:
        merged = calendars[0] if len(calendars) == 1 else MergedCalendar(calendars)
        registry.extend(build_calendar_tools(merged, settings.timezone))
    mailboxes = [ImapMailbox(account) for account in settings.mail_accounts]
    registry.extend(build_email_tools(mailboxes, settings.timezone))
    registry.extend(build_draft_tools([m for m in mailboxes if m.writable], settings.language))
    if mailboxes and settings.deliveries_enabled:
        deliveries = Deliveries(Path(settings.data_dir) / "deliveries.json")
        registry.extend(build_delivery_tools(deliveries))
    if settings.travel_enabled:
        router = Router(
            http,
            language=settings.language,
            geocoding_url=settings.nominatim_url,
            routing_url=settings.osrm_url,
        )
        registry.extend(
            build_travel_tools(
                router,
                home=home,
                timezone=settings.timezone,
                margin_minutes=settings.travel_margin_minutes,
            )
        )
    if settings.hosting_provider and settings.hosting_api_token:
        hosting = build_hosting_provider(
            settings.hosting_provider, settings.hosting_api_token.get_secret_value(), http
        )
        registry.extend(build_hosting_tools(hosting, settings.language))
    if settings.github_token:
        token = settings.github_token.get_secret_value()
        registry.extend(build_github_tools(GitHub(http, token)))
        add_notes(registry, settings, http, token)
    if reaches := reports_reach(settings):
        registry.extend(
            build_task_tools(
                build_task_folder(settings, http),
                settings.timezone,
                settings.language,
                built_in_tasks(
                    strings(settings.language),
                    settings.morning_briefing_at,
                    settings.evening_recap_at,
                ),
                reaches,
            )
        )
    registry.extend(build_connector_tools(build_connectors(settings, http)))
    if settings.kb_url and settings.kb_token:
        kb = KnowledgeBase(http, settings.kb_url, settings.kb_token.get_secret_value())
        registry.extend(build_knowledge_tools(kb))
    if settings.drives_enabled:
        add_drives(registry, settings, http, llm)
    if settings.web_search_enabled and settings.web_search_backend == "native":
        registry.extend(
            build_web_tools(
                llm,
                model=settings.web_search_model,
                max_uses=settings.web_search_max_uses,
                city=settings.home_city,
                country=settings.home_country,
                timezone=settings.timezone,
                language=settings.language,
                engine=subscription_engine(settings, settings.web_search_model),
            )
        )
    elif settings.web_search_enabled:
        key = settings.web_search_api_key
        engine = build_search_engine(
            settings.web_search_backend,
            http,
            url=settings.web_search_url,
            api_key=key.get_secret_value() if key else None,
            language=settings.language,
            country=settings.home_country,
        )
        registry.extend(
            build_engine_web_tools(
                # The answer written from the results: on the Claude subscription first.
                SubscriptionFirst(subscription_engine(settings, settings.web_search_model), llm),
                engine,
                model=settings.web_search_model,
                max_results=settings.web_search_results,
                language=settings.language,
            )
        )
    registry.extend(build_document_side_tools(settings, http, llm))
    # What the agent keeps: facts about the user, how-tos, alerts turned down, the journal.
    kept = kept or build_kept(settings, http)
    registry.extend(build_memory_tools(kept.memory, settings.language))
    registry.extend(
        build_procedure_tools(
            kept.procedures, settings.timezone, settings.assistant_name, settings.language
        )
    )
    registry.extend(build_turned_down_tools(kept.turned_down))
    registry.extend(build_past_conversation_tools(kept.journal))
    if settings.web_pages_enabled:
        trusted = trusted_domains(settings.home_country, settings.web_trusted_domains)
        registry.extend(build_web_page_tools(WebPages(trusted), language=settings.language))
    browser = build_browser(settings, http)
    registry.extend(build_browser_tools(browser))
    if browser is not None and browser.vpn is not None:
        # gluetun connects as it starts: the VPN is stopped whenever nothing browses abroad.
        registry.jobs.append(browser.keep_vpn_off)
    if settings.investigations_enabled:
        investigator = Investigator(http, nominatim_url=settings.nominatim_url)
        registry.extend(build_osint_tools(investigator, settings.public_url))
    # Coding jobs (docs/jobs.md): the runner is built here for its tools; lifespan gives
    # it where its reports go and starts following the sessions still open.
    registry.llm = llm
    registry.job_runner = build_job_runner(settings, http)
    registry.extend(build_job_tools(registry.job_runner, settings.language))
    if settings.whatsapp_enabled and settings.whatsapp_reading_enabled and http is not None:
        # On the user's own account, whichever session the chat itself goes through.
        waha = Waha(
            http, settings.waha_url, settings.waha_api_key.get_secret_value(), settings.waha_session
        )
        transcriber = build_transcriber(settings)
        registry.extend(
            build_whatsapp_chat_tools(
                waha,
                settings.whatsapp_readable_chats,
                settings.timezone,
                language=settings.language,
                transcribe=transcriber.transcribe if transcriber else None,
            )
        )
    # The core's own tools, last: the writes waiting for the user's go (the registry parks
    # them whatever is configured) and the long results read part by part.
    registry.extend(
        build_proposal_tools(registry, language=settings.language, polls=settings.chat_enabled)
    )
    registry.extend(build_long_result_tools())
    log.info("tools: %s", ", ".join(d["name"] for d in registry.definitions()))
    return registry


def build_document_side_tools(
    settings: Settings, http: httpx.AsyncClient, llm: LLMClient
) -> list[Tool]:
    """Documents, PDFs, drawings, generated pictures and the code sandbox
    (docs/documents.md), each only when it is switched on and can work here."""
    language = settings.language
    tools: list[Tool] = []
    if settings.documents_enabled:
        fonts = Path(settings.pdf_font_dir) if settings.pdf_font_dir else FONT_DIR
        paper = paper_for(settings.paper_size, settings.home_country)
        tools += build_document_tools(
            fonts, paper=paper, language=language, country=settings.home_country
        )
        tools += build_pdf_form_tools(fonts, language=language)
        tools += build_pdf_page_tools()
        bank = http if settings.exercise_photos_enabled else None
        tools += build_illustration_tools(bank, fonts, language=language)
    providers: list[Provider] = []
    if key := settings.generation_gemini_api_key:
        providers.append(
            Gemini(
                http,
                key.get_secret_value(),
                _model(settings.generation_gemini_image_model),
                _model(settings.generation_gemini_video_model),
            )
        )
    if settings.generation_openai_api_key or settings.generation_openai_base_url:
        key = settings.generation_openai_api_key
        providers.append(
            OpenAI(
                http,
                key.get_secret_value() if key else "",
                _model(settings.generation_openai_image_model),
                _model(settings.generation_openai_video_model),
                settings.generation_openai_base_url or OPENAI_URL,
            )
        )
    providers.sort(key=lambda p: p.name != settings.generation_default_provider)
    tools += build_generation_tools(
        providers,
        settings.generation_max_images,
        settings.generation_max_videos,
        language=language,
    )
    if settings.code_execution_enabled:
        # Anthropic only: the other providers have no sandbox, so run_code stays off.
        files = getattr(llm, "files", None) if settings.llm_provider == ANTHROPIC else None
        if files is None:
            log.warning("CODE_EXECUTION_ENABLED needs LLM_PROVIDER=anthropic: run_code is off")
        model = settings.code_execution_model or settings.llm_model
        tools += build_code_tools(llm, files, model, language=language)
    return tools


def _model(name: str) -> str:
    """A model's name, or nothing when the setting switches that kind off."""
    return "" if name.strip().casefold() in {"off", "none"} else name.strip()


def add_drives(
    registry: ToolRegistry, settings: Settings, http: httpx.AsyncClient, llm: LLMClient
) -> None:
    """The file drives of the rclone sidecar: their tools, and the jobs that keep the
    index of names and the index of contents fresh."""
    data = Path(settings.data_dir)
    assert settings.drive_url and settings.drive_password
    trash = LocalTrash(
        data / "drive-trash",
        keep_days=settings.drive_trash_days,
        max_item_bytes=settings.drive_trash_max_item_mb * 1024**2,
        max_total_bytes=settings.drive_trash_max_total_mb * 1024**2,
    )
    drives = build_drives(
        http,
        settings.drive_url,
        settings.drive_user,
        settings.drive_password.get_secret_value(),
        settings.drives,
        data_dir=data,
        trash=trash,
        index_max_age=settings.drive_index_hours * 3600,
    )
    registry.extend(build_drive_tools(drives, language=settings.language))
    registry.jobs += [drive.keep_index_fresh for drive in drives]
    if not settings.drive_content:
        return
    if settings.drive_ocr == "model" and not settings.drive_ocr_enabled:
        log.warning(
            "DRIVE_OCR=model needs a provider that reads PDFs, not %s: scanned PDFs stay "
            "listed by name, only text layers are indexed.",
            settings.llm_provider,
        )
    ocr = None
    if settings.drive_ocr_enabled:
        plan = subscription_engine(settings, settings.drive_ocr_model)
        ocr = model_ocr(llm, settings.drive_ocr_model, plan)
    contents = build_drive_contents(drives, data, ocr)
    registry.extend(build_drive_content_tools(contents, ocr=ocr is not None))
    for drive in drives:
        if drive.name in contents:
            registry.jobs.append(
                lambda d=drive: contents[d.name].keep_fresh(lambda: d.index)  # type: ignore[misc]
            )


def build_browser(settings: Settings, http: httpx.AsyncClient) -> RemoteBrowser | None:
    """The remote Chrome the user logs into (docs/browser.md), with its VPN exit when
    compose/vpn.yml is there."""
    if not settings.browser_cdp_host:
        return None
    if not settings.browser_public_url:
        log.error("The remote browser needs BROWSER_PUBLIC_URL, the address of its page")
        return None
    if not browser_available():
        log.error('BROWSER_CDP_HOST needs the playwright package: pip install ".[browser]"')
        return None
    key = settings.vpn_control_key
    return RemoteBrowser(
        settings.browser_cdp_host,
        settings.browser_cdp_port,
        settings.browser_public_url,
        Path(settings.browser_downloads_dir),
        Path(settings.data_dir) / "browser-sites.json",
        trusted_domains(settings.home_country, settings.web_trusted_domains),
        vpn=Vpn(http, settings.vpn_control_url, key.get_secret_value()) if key else None,
        abroad_proxy=settings.browser_abroad_proxy or "",
    )


def build_icloud(settings: Settings) -> ICloudWeb | None:
    """The iCloud web session behind Reminders, Notes and Find My (docs/apple.md)."""
    if not settings.icloud_apple_id:
        return None
    if not icloud_available():
        log.error('ICLOUD_APPLE_ID needs the pyicloud package: pip install ".[icloud]"')
        return None
    return ICloudWeb(settings.icloud_apple_id, session_dir(settings.data_dir))


def build_connectors(settings: Settings, http: httpx.AsyncClient) -> list[Connector]:
    connectors = []
    for server in settings.mcp_servers:
        label = server.label or server.name
        auth: TokenSource = NoAuth()
        if server.token:
            auth = StaticToken(server.token.get_secret_value())
        elif server.client_id and server.token_url:
            secret, refresh = server.client_secret, server.refresh_token
            auth = OAuthToken(
                http,
                label,
                server.token_url,
                server.client_id,
                client_secret=secret.get_secret_value() if secret else None,
                refresh_token=refresh.get_secret_value() if refresh else None,
                resource=server.resource,
                scope=server.scope,
                store=Path(settings.data_dir) / f"mcp-{server.name}.json",
            )
        client = MCPClient(http, auth, server.url, label)
        connectors.append(
            Connector(
                server.name,
                client,
                writes=server.writes == "confirm",
                also_read=frozenset(server.read_tools),
                language=settings.language,
            )
        )
    return connectors


def prompt_config(settings: Settings) -> PromptConfig:
    spots = [*settings.wind_spots, *settings.surf_spots]
    return PromptConfig(
        assistant_name=settings.assistant_name,
        language=settings.language,
        timezone=settings.timezone,
        activities=[CATALOG[key] for key in settings.outdoor_activities],
        brief_sections=settings.brief_sections,
        weather_detail=settings.weather_detail,
        spot_example=spots[0].name if spots else None,
        user_name=settings.user_name,
        browser_abroad=bool(settings.vpn_control_key and settings.browser_abroad_proxy),
    )


def build_pet_alerts(settings: Settings, devices: dict) -> PetAlerts | None:
    """The pet supplies watch, when the PetKit tools and a push channel are set up."""
    reached = settings.ntfy_url or settings.chat_enabled
    if not (settings.petkit_alerts and reached and "petkit" in devices):
        return None
    return PetAlerts(
        devices["petkit"].status,
        Path(settings.data_dir) / "pet-alerts.json",
        settings.timezone,
        language=settings.language,
        litter_percent=settings.petkit_litter_alert_percent,
        food_percent=settings.petkit_food_alert_percent,
        remind_minutes=settings.petkit_bin_remind_minutes,
        check_minutes=settings.petkit_check_minutes,
        quiet=(settings.petkit_quiet_start, settings.petkit_quiet_end),
    )


def build_journal(settings: Settings) -> Journal | None:
    if not settings.journal_keep_days:
        return None
    directory = Path(settings.data_dir) / "journal"
    return Journal(directory, settings.timezone, settings.journal_keep_days)


@dataclass
class Kept:
    """What the agent keeps between conversations (docs/memory.md)."""

    memory: Memory | None = None
    procedures: Memory | None = None
    turned_down: TurnedDown | None = None
    journal: Journal | None = None

    async def prompt_text(self) -> str:
        """For the system prompt: the facts kept, and the alerts the user turned down."""
        facts = await self.memory.prompt_text() if self.memory else ""
        turned_down = self.turned_down.prompt_text() if self.turned_down else ""
        return "\n\n".join(p for p in (facts, turned_down) if p)


def memory_paths(settings: Settings) -> tuple[str, str]:
    """Where the memory and the procedures are: files of DATA_DIR, or paths of the
    repository in a folder named after the assistant."""
    if settings.memory_store == "local":
        base = Path(settings.data_dir)
        return (
            str(base / (settings.memory_path or "memory.md")),
            str(base / (settings.procedures_path or "procedures.md")),
        )
    folder = folder_name(settings.assistant_name)
    return (
        (settings.memory_path or f"{folder}/memory.md").strip("/"),
        (settings.procedures_path or f"{folder}/procedures.md").strip("/"),
    )


def tasks_path(settings: Settings) -> str:
    """Where the tasks are: a folder of DATA_DIR, or of the notes repository, there in a
    folder named after the assistant."""
    if settings.tasks_store == "local":
        return str(Path(settings.data_dir) / (settings.tasks_path.strip("/") or "tasks"))
    return (settings.tasks_path or f"{folder_name(settings.assistant_name)}/tasks").strip("/")


def _crypt_repos(settings: Settings) -> dict[str, tuple[SecretStr, list[str]]]:
    """Each repository the agent has a git-crypt key for, with the paths it writes there."""
    repos: dict[str, tuple[SecretStr, list[str]]] = {}
    if settings.memory_store == "github" and settings.memory_repo and settings.memory_crypt_key:
        repos[settings.memory_repo] = (settings.memory_crypt_key, list(memory_paths(settings)))
    if settings.notes_repo:
        key = settings.notes_crypt_key
        if key is None and settings.notes_repo in repos:
            key = repos[settings.notes_repo][0]
        if key is not None:
            paths = repos.get(settings.notes_repo, (key, []))[1]
            paths += [
                f"{settings.assistant_reminders_path.strip('/')}/x",
                f"{settings.handoffs_path.strip('/')}/x",
            ]
            if settings.tasks_store == "github":
                paths.append(f"{tasks_path(settings)}/x")
            repos[settings.notes_repo] = (key, paths)
    return repos


def build_crypt(settings: Settings, repo: str | None = None) -> ConfigCrypt | None:
    """The git-crypt codec of a repository the agent has a key for (the memory's by
    default, or the notes repository); its perimeter is read from the repository's
    .gitattributes at startup (load_crypt_patterns)."""
    if repo is None:
        repo = settings.memory_repo if settings.memory_store == "github" else None
    if repo is None or repo not in (repos := _crypt_repos(settings)):
        return None
    key, paths = repos[repo]
    # Until the repository's .gitattributes is read, the agent's own folders count as
    # encrypted: a write made before that, or after a failed read, is never in clear by
    # default.
    own = dict.fromkeys(f"{path.rsplit('/', 1)[0]}/**" if "/" in path else path for path in paths)
    return _crypt_for(key.get_secret_value(), tuple(own))


@lru_cache
def _crypt_for(key: str, own: tuple[str, ...]) -> ConfigCrypt | None:
    if not crypt_available():
        log.error('A git-crypt key needs the cryptography package: pip install ".[crypt]"')
        return None
    try:
        return ConfigCrypt.from_base64(key, list(own))
    except CryptError as exc:
        log.error("MEMORY_CRYPT_KEY or NOTES_CRYPT_KEY is not a git-crypt key (%s)", exc)
        return None


async def load_crypt_patterns(settings: Settings, http: httpx.AsyncClient) -> None:
    for repo in _crypt_repos(settings):
        crypt = build_crypt(settings, repo)
        if crypt is None or not settings.github_token:
            continue
        try:
            resp = await http.get(
                f"{API_URL}/repos/{repo}/contents/.gitattributes",
                headers={
                    "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("could not read the .gitattributes of %s (%s)", repo, exc)
            continue
        patterns = patterns_from_attributes(resp.text)
        if patterns:
            crypt.patterns = patterns
        log.info("encrypted folders of %s: %s", repo, ", ".join(crypt.patterns))


def build_kept(settings: Settings, http: httpx.AsyncClient) -> Kept:
    kept = Kept(
        turned_down=TurnedDown(
            Path(settings.data_dir) / "turned-down-alerts.json", settings.timezone
        ),
        journal=build_journal(settings),
    )
    memory_path, procedures_path = memory_paths(settings)
    name, timezone = settings.assistant_name, settings.timezone
    if settings.memory_store == "local":
        kept.memory = Memory(LocalFile(Path(memory_path)), timezone, name)
        kept.procedures = Memory(LocalFile(Path(procedures_path)), timezone, name)
    elif settings.memory_store == "github" and settings.github_token and settings.memory_repo:
        crypt = build_crypt(settings)
        if settings.memory_crypt_key and crypt is None:
            # Never fall back to writing in clear where a key says the folder is encrypted.
            log.error("memory is off: MEMORY_CRYPT_KEY is set but cannot be used")
            return kept
        token, repo = settings.github_token.get_secret_value(), settings.memory_repo
        kept.memory = Memory(GitHubFile(http, token, repo, memory_path, crypt), timezone, name)
        kept.procedures = Memory(
            GitHubFile(http, token, repo, procedures_path, crypt), timezone, name
        )
    return kept


def add_notes(
    registry: ToolRegistry, settings: Settings, http: httpx.AsyncClient, token: str
) -> None:
    """The notes repository (reminders and handoff notes for the user's coding sessions)
    and, when asked for, the user's repositories read-only."""
    repo = settings.notes_repo
    crypt = build_crypt(settings, repo) if repo else None
    if repo and crypt is None and repo in _crypt_repos(settings):
        # A key that can't be used: nothing is read as ciphertext nor written in clear.
        log.error("the notes repository is off: its git-crypt key cannot be used")
        repo = None
    if repo:
        reminders = ReminderRepo(http, token, repo, settings.assistant_reminders_path, crypt=crypt)
        registry.extend(build_assistant_reminder_tools(reminders, settings.timezone))
        if settings.notes_write:
            registry.extend(
                build_assistant_note_tools(
                    notes_repo(settings, http),
                    settings.timezone,
                    settings.language,
                    settings.assistant_reminders_path,
                    settings.handoffs_path,
                )
            )
    if settings.github_repositories:
        repos = Repositories(http, token, repo, crypt=crypt)
        registry.extend(build_repository_tools(repos))
        registry.extend(build_vulnerability_tools(Vulnerabilities(http, repos)))


def notes_repo(settings: Settings, http: httpx.AsyncClient) -> NotesRepo | None:
    if not (settings.github_token and settings.notes_repo):
        return None
    return NotesRepo(
        http,
        settings.github_token.get_secret_value(),
        settings.notes_repo,
        crypt=build_crypt(settings, settings.notes_repo),
        author=folder_name(settings.assistant_name),
    )


def reports_reach(settings: Settings) -> str | None:
    """How a report the agent writes by itself reaches the user, in a few words for the
    model; None when nothing does, and then there are no tasks. A new channel
    (deliver.ReportChannel) is named here and added in build_delivery."""
    if settings.tasks_store == "off":
        return None
    if settings.chat_enabled:
        return ChatReports.reaches
    return NtfyReports.reaches if settings.ntfy_url else None


def alerts_reach(settings: Settings) -> str | None:
    """How a line the code writes by itself (an activity alert) reaches the user, as
    tell_user sends it; None when nothing does, and then there are no alerts."""
    if settings.chat_enabled:
        return ChatReports.reaches
    return NtfyReports.reaches if settings.ntfy_url else None


def build_delivery(
    settings: Settings, ntfy: Ntfy | None, chats: dict[str, ChatSession] | None = None
) -> Delivery:
    """A chat first, where the user can answer and approve; ntfy after it, for a report
    the chat could not take."""
    channels: list[ReportChannel] = [ChatReports(session) for session in (chats or {}).values()]
    if ntfy is not None:
        channels.append(NtfyReports(ntfy, settings.assistant_name))
    return Delivery(channels, Outbox(Path(settings.data_dir) / "task-outbox.json"))


def jobs_reach(settings: Settings) -> bool:
    """Whether a coding job's report can reach the user: without a way, there are no jobs.
    A new channel of build_delivery is named here too."""
    return bool(settings.chat_enabled or settings.ntfy_url)


def build_job_runner(settings: Settings, http: httpx.AsyncClient) -> JobRunner | None:
    """The coding jobs' runner (docs/jobs.md), None unless a runner is set up and its
    reports can reach the user."""
    if not settings.jobs_enabled:
        return None
    if not jobs_reach(settings):
        log.warning(
            "coding jobs: off, nothing reaches the user for their reports (a chat, NTFY_URL)"
        )
        return None
    s = strings(settings.language)
    backend: Backend
    if settings.jobs_runner == "claude_code":
        backend = ClaudeCodeBackend(
            http, settings.coder_url, settings.coder_token.get_secret_value()
        )
        repo_path = "/work/{job}/{name}"
    else:
        # Anthropic's own API (Managed Agents), not a model call: no other provider has it.
        from anthropic import AsyncAnthropic

        from intelligent_home_agent.jobs_managed import ManagedAgentsBackend

        backend = ManagedAgentsBackend(
            AsyncAnthropic(api_key=settings.llm_api_key.get_secret_value()).beta.sessions,
            agent_id=settings.jobs_agent_id,
            environment_id=settings.jobs_environment_id,
            github_token=settings.jobs_github_token.get_secret_value(),
            vault_ids=[settings.jobs_vault_id] if settings.jobs_vault_id else None,
            budget_usd=settings.jobs_budget_usd,
            max_iterations=settings.jobs_max_iterations,
            workspace=settings.jobs_workspace,
            texts=s,
        )
        repo_path = "/workspace/{name}"

    async def unsent(job: Job, text: str) -> None:
        log.warning("Job %s reported before the app was up", job.id)

    log.info("coding jobs: on, run by %s", settings.jobs_runner)
    return JobRunner(
        backend,
        JobStore(Path(settings.data_dir) / "jobs.json"),
        report=unsent,
        repo_path=repo_path,
        git_name=settings.jobs_git_name or "",
        git_email=settings.jobs_git_email or "",
        user_name=settings.user_name or "the user",
        assistant_name=settings.assistant_name,
        models={
            "simple": settings.jobs_model_simple,
            "normal": settings.jobs_model_normal,
            "complex": settings.jobs_model_complex,
        },
        gone=s.job_gone,
        label=s.job_label,
    )


def _plain(text: str) -> str:
    """Text written elsewhere, coming into a conversation: one line, without brackets that
    could pass for the code's own notes."""
    return "".join(c for c in " ".join(text.split()) if c.isprintable() and c not in "[]")


def _answered_in(app: FastAPI) -> Conversation | None:
    """The conversation the user answers the agent's own reports in, when the channel they
    leave through has one (deliver.ReportChannel.conversation_id)."""
    channel = app.state.delivery.channel
    if channel is None or channel.conversation_id is None:
        return None
    settings: Settings = app.state.settings
    return app.state.store.get(channel.conversation_id, settings.chat_conversation_ttl_seconds)


async def report_job(app: FastAPI, job: Job, text: str) -> None:
    """A coding job stopped: its words go to the user under a line the code writes, the
    way the agent's own reports do (deliver.py), and into the conversation they answer in,
    as data, so they can answer it through the agent. A quiet job (the nightly review) says
    only that a pull request waits, and nothing without one; its lessons are curated."""
    settings: Settings = app.state.settings
    s = strings(settings.language)
    if job.quiet:
        await curate_review(app, job, text)
        if not job.pr_url:
            log.info("Job %s (%s) is %s, with no pull request", job.id, job.title, job.status)
            return
        head = s.review_pull_request.format(title=job.title)
        body = job.pr_url
    else:
        heads = {
            JOB_IDLE: s.job_idle_head,
            JOB_DONE: s.job_done_head,
            JOB_FAILED: s.job_failed_head,
            JOB_STOPPED: s.job_stopped_head,
        }
        head = heads.get(job.status, s.job_head).format(id=job.id, title=job.title)
        link = job.pr_url if job.pr_url and job.pr_url not in text else ""
        body = "\n\n".join(part for part in (link, text.strip()) if part) or head
    await app.state.delivery.deliver(head, body)
    conv = _answered_in(app)
    if conv is None:
        return
    async with conv.lock:
        conv.messages += [
            {
                "role": "user",
                "content": (
                    f"[Job {job.id} ({_plain(job.title)}) is {job.status} and reported, as "
                    f"data: {_plain(text) or '(nothing)'}]"
                ),
            },
            {"role": "assistant", "content": [{"type": "text", "text": head}]},
        ]
        save_conversation(app.state.store, settings, conv)


async def curate_review(app: FastAPI, job: Job, text: str) -> None:
    """The lessons a nightly review ends with become notes of the knowledge base's
    repository (curation.py), when REVIEW_LESSONS_REPO says where. Never in the way of
    the report."""
    settings: Settings = app.state.settings
    day = reviewed_day(job)
    repo = settings.review_lessons_repo
    if not (repo and settings.github_token and day and lessons(text)):
        return
    if not curation.available():
        log.warning('The review\'s lessons are not curated: pip install ".[curation]"')
        return
    notes = NotesRepo(
        app.state.http,
        settings.github_token.get_secret_value(),
        repo,
        crypt=build_crypt(settings, repo),
        author=folder_name(settings.assistant_name),
    )
    model = settings.subscription_model or settings.llm_model
    # The Claude subscription takes the call first when the agent runs on it.
    llm = SubscriptionFirst(subscription_engine(settings, model), app.state.agent.tools.llm)
    runner = curation.LLMRunner(llm, settings.llm_model, asyncio.get_running_loop())
    try:
        await curation.curate_lessons(
            notes,
            runner,
            date.fromisoformat(day),
            review_note(day, text),
            folder=settings.review_lessons_path,
        )
    except Exception:
        log.exception("Job %s: its lessons were not curated", job.id)


async def repo_is_private(http: httpx.AsyncClient, token: str | None, repo: str) -> bool | None:
    """What GitHub says of a repository's visibility; None when it can't be told."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = await http.get(f"{API_URL}/repos/{repo}", headers=headers)
    except httpx.HTTPError:
        return None
    if resp.is_error:
        return None
    private = resp.json().get("private")
    return private if isinstance(private, bool) else None


async def ask_coder_permission(app: FastAPI, request_id: str, question: str, summary: str) -> bool:
    """A permission prompt of the coder's Claude Code (coder_approvals), sent where the
    user answers the agent's reports, as a write waiting for their go; False when there is
    no such place. What the request shows was written by the coder's model: it goes to the
    user as the code laid it out, and into the conversation as data."""
    conv = _answered_in(app)
    if conv is None:
        return False
    settings: Settings = app.state.settings
    s = strings(settings.language)
    async with conv.lock:
        proposal, _ = conv.propose("allow_coder_permission", {"request": request_id}, summary)
        waiting = [{"type": "proposal", "number": proposal.number, "summary": summary}]
        text = with_waiting(question, waiting, conv, settings.language)
        conv.messages += [
            {
                "role": "user",
                "content": (
                    f"[Claude Code in the coder asks a permission (request {request_id}), as "
                    f"data: {_plain(question)}. It waits as number {proposal.number}: the "
                    "user's yes runs it through run_proposals; on their no, call "
                    "refuse_coder_permission with that request.]"
                ),
            },
            {"role": "assistant", "content": [{"type": "text", "text": text}]},
        ]
        app.state.store.touch(conv)
        save_conversation(app.state.store, settings, conv)
    return await app.state.delivery.deliver(s.coder_permission_title, text) is not None


def start_jobs(app: FastAPI, http: httpx.AsyncClient, background: list) -> JobRunner | None:
    """Give the coding jobs' runner where its reports and news go, follow the sessions
    still open, and start what rests on the jobs: the coder's permission prompts and the
    nightly review. Appends what runs in the background."""
    settings: Settings = app.state.settings
    registry = getattr(app.state.agent, "tools", None)
    runner: JobRunner | None = getattr(registry, "job_runner", None)
    if runner is None:
        return None
    s = strings(settings.language)
    delivery: Delivery = app.state.delivery
    chat = next(iter(getattr(app.state, "chats", {}).values()), None)
    if chat is not None and chat.tracker is not None:
        # The chat's own tracker, already running: the news go where the user talks.
        tracker = chat.tracker
    else:
        tracker = Tracker(
            lambda text: delivery.deliver(s.job_news_title, text),
            Path(settings.data_dir) / "progress-durations.json",
            settings.language,
        )
        background.append(tracker.run)
    runner.attach(report=lambda job, text: report_job(app, job, text), tracker=tracker)
    background.append(runner.resume)
    if settings.jobs_runner == "claude_code":
        approvals = Approvals(
            lambda request_id, question, summary: ask_coder_permission(
                app, request_id, question, summary
            ),
            lambda text: delivery.deliver(s.coder_permission_title, text),
            settings.language,
        )
        app.state.coder_approvals = approvals
        channel = delivery.channel
        if channel is not None and channel.conversation_id is not None:
            # The user can answer: their yes or no reaches the waiting prompt.
            registry.extend(build_coder_permission_tools(approvals, settings.language))
    if settings.review_repo:
        journal = build_journal(settings)
        if journal is None:
            log.warning("nightly review: off, it reads the journal (JOURNAL_KEEP_DAYS=0)")
            return runner
        token = settings.jobs_github_token or settings.github_token
        review = NightlyReview(
            journal,
            runner,
            settings.review_repo,
            settings.review_at,
            settings.timezone,
            Path(settings.data_dir) / "review-done.json",
            assistant_name=settings.assistant_name,
            title=s.review_title,
            is_private=lambda: repo_is_private(
                http, token.get_secret_value() if token else None, settings.review_repo
            ),
            allow_public=settings.review_allow_public,
        )
        background.append(review.run)
        log.info(
            "nightly review: on %s, after %s",
            settings.review_repo,
            settings.review_at.strftime("%H:%M"),
        )
    return runner


async def tell_user(app: FastAPI, title: str, text: str) -> None:
    """A line the code writes by itself (an alert): in the chat when there is one, else
    as a push notification."""
    for session in app.state.chats.values():
        try:
            await session.channel.send(text)
            return
        except httpx.HTTPError as exc:
            log.warning("Could not say it in the chat (%s): %s", session.name, exc)
    if app.state.ntfy is not None:
        await app.state.ntfy.send(title, text)


def build_task_folder(settings: Settings, http: httpx.AsyncClient) -> NoteFolder | None:
    if settings.tasks_store == "local":
        return LocalFolder(Path(tasks_path(settings)))
    repo = notes_repo(settings, http) if settings.tasks_store == "github" else None
    if repo is None:
        return None
    crypt = build_crypt(settings, settings.notes_repo)
    if crypt is None and settings.notes_repo in _crypt_repos(settings):
        log.error("tasks are off: the notes repository's git-crypt key cannot be used")
        return None
    reader = ReminderRepo(
        http,
        settings.github_token.get_secret_value(),
        settings.notes_repo,
        tasks_path(settings),
        crypt=crypt,
    )
    return RepoFolder(repo, reader, tasks_path(settings))


def build_task_runner(app: FastAPI, http: httpx.AsyncClient) -> TaskRunner | None:
    settings: Settings = app.state.settings
    if reports_reach(settings) is None:
        return None
    folder = build_task_folder(settings, http)
    if folder is None:
        return None
    return TaskRunner(
        folder,
        lambda title, body, repeating, fresh=False: run_task(
            app, title, body, repeating, fresh=fresh
        ),
        Path(settings.data_dir) / "tasks-done.json",
        settings.timezone,
        settings.tasks_poll_seconds,
        folder.close,
        folder.close_for_day,
        lambda title, body, gathered: run_digest(app, title, body, gathered),
        built_in_tasks(
            strings(settings.language), settings.morning_briefing_at, settings.evening_recap_at
        ),
    )


def task_reports_file(settings: Settings) -> Path:
    return Path(settings.data_dir) / "tasks-reports.json"


async def answer_task(
    app: FastAPI,
    text: str,
    *,
    quiet: bool = False,
    held: list[dict] | None = None,
    may_be_silent: bool = False,
    wants_clean: list[bool] | None = None,
) -> str | None:
    """Answer a task the user left, as if they had just asked; the reply is written for
    the channel it leaves through (deliver.py).

    ``quiet`` (a repeating task) returns None, leaving no trace, when the agent answers
    SILENT_REPLY. Such a run only finds and proposes: it starts tainted, as it carries its
    last report, written from what an earlier run read, so no write can happen in it, let
    alone one hidden by its silence.
    ``held`` collects the drafts and waiting writes instead of listing them in the reply,
    for a digest that lists them once under its own message.
    ``may_be_silent``: a SILENT_REPLY leaves no trace, as in a quiet run, but the turn is
    not tainted for it (a fresh task).
    ``wants_clean`` gets True when a repeating run was refused a tool that the task's
    clean runs would get (tasks.py).
    """
    settings: Settings = app.state.settings
    store: ConversationStore = app.state.store
    channel = app.state.delivery.channel
    # Where the user can answer the report, what waits for their go is parked in that
    # conversation; through a one-way channel nothing is parked, and each run starts from
    # a conversation of its own, as a scheduler's briefing does.
    conversation_id = channel.conversation_id if channel is not None else None
    if conversation_id is not None:
        conv = store.get(conversation_id, settings.chat_conversation_ttl_seconds)
    else:
        conv = Conversation(id="task")
    async with conv.lock:
        if conversation_id is not None:
            conv.kept = True
            keep_files(settings, conv)
        before = list(conv.messages)
        try:
            reply = await app.state.agent.ask(
                conv,
                text,
                display=channel.display if channel is not None else "notification",
                scheduled=True,
                tainted=quiet,
                answerable=conversation_id is not None,
            )
        except Exception:
            log.exception("A task's answer failed")
            conv.drop_unshown()
            return strings(settings.language).fallback_reply
        if wants_clean is not None and reply.wants_clean_runs:
            wants_clean.append(True)
        if (quiet or may_be_silent) and says_nothing(reply.text) and not reply.actions:
            conv.messages = before
            return None
        # A quiet run with nothing to say still lists what it did or asks: never the
        # silent marker itself.
        said = "" if says_nothing(reply.text) and reply.actions else reply.text
        if held is None:
            said = with_waiting(
                said, reply.actions, conv, settings.language, _option_chars(channel)
            ).strip()
        else:
            held.extend(
                a for a in reply.actions if a.get("type") in {"draft", "written", "proposal"}
            )
        if conversation_id is not None:
            store.touch(conv)
            save_conversation(store, settings, conv)
    if (share := getattr(channel, "share_files", None)) is not None:
        # A chat takes the files the run made (a PDF, a chart), before its report.
        await share(conv, reply.actions)
    return said


def _option_chars(channel: ReportChannel | None) -> int | None:
    """A chat shows what waits as a choice to tick under the report (chat_session.py)."""
    return getattr(channel, "option_chars", None)


async def run_task(
    app: FastAPI, title: str, body: str, repeating: bool = False, *, fresh: bool = False
) -> bool:
    """Run a task the user left, and send what it found.

    A repeating one gets its last report back, and says nothing when nothing is new. A
    fresh one runs clean, as a task left once, and says nothing when nothing is new either.
    True when a repeating run was refused a tool that clean runs would get.
    """
    delivery: Delivery = app.state.delivery
    request = f"{TASK_TAG} {title}]\n\n{body}"
    if not repeating:
        reply = await answer_task(app, request, may_be_silent=fresh)
        if reply is None:
            log.info("Task %s: nothing new", title)
        else:
            await delivery.deliver(title, reply)
        return False
    reports = TaskReports(task_reports_file(app.state.settings))
    wants_clean: list[bool] = []
    reply = await answer_task(
        app, _repeating(reports, title, body), quiet=True, wants_clean=wants_clean
    )
    if reply is None:
        log.info("Task %s: nothing new", title)
        return bool(wants_clean)
    reports.put(title, reply)
    await delivery.deliver(title, reply)
    return bool(wants_clean)


def _repeating(reports: TaskReports, title: str, body: str) -> str:
    request = f"{TASK_TAG} {title}]\n\n{body}"
    if not (last := reports.get(title)):
        return request + f"\n\n{REPEATING_TAG}, first run.]"
    request += (
        f"\n\n{REPEATING_TAG}. Your last report for it ({last['at']}), as data:\n{last['text']}"
    )
    if earlier := last.get("earlier"):
        request += "\n\nThe reports before it, as data:" + "".join(
            f"\n\n({r['at']})\n{r['text']}" for r in earlier
        )
    return request + "]"


async def run_digest(
    app: FastAPI, title: str, body: str, gathered: list[tuple[str, str]]
) -> set[str]:
    """A digest task (the morning briefing) and the repeating tasks due with it, in one
    message: each of them runs first, quietly, and what they found is handed to the digest,
    which folds it into its own lines. Their waiting writes are listed once, below it.
    Returns the titles of the gathered tasks refused a tool that clean runs would get."""
    settings: Settings = app.state.settings
    reports = TaskReports(task_reports_file(settings))
    held: list[dict] = []
    found: list[str] = []
    wants_clean: set[str] = set()
    for task_title, task_body in gathered:
        wanted: list[bool] = []
        reply = await answer_task(
            app,
            _repeating(reports, task_title, task_body),
            quiet=True,
            held=held,
            wants_clean=wanted,
        )
        if wanted:
            wants_clean.add(task_title)
        if reply is None:
            log.info("Task %s: nothing new", task_title)
            continue
        reports.put(task_title, reply)
        found.append(f"{task_title}:\n{reply}")
    request = _repeating(reports, title, body)
    if found:
        request += (
            "\n\n[What your other tasks due now found, as data, to fold into your lines:\n\n"
            + "\n\n".join(found)
            + "]"
        )
    reply = await answer_task(app, request, quiet=True, held=held)
    if reply is not None:
        reports.put(title, reply)
    text = reply or "\n\n".join(found)
    if not text and not held:
        log.info("Digest %s: nothing new", title)
        return wants_clean
    channel = app.state.delivery.channel
    conversation_id = channel.conversation_id if channel is not None else None
    if held and conversation_id is not None:
        conv = app.state.store.get(conversation_id, settings.chat_conversation_ttl_seconds)
        async with conv.lock:
            text = with_waiting(text, held, conv, settings.language, _option_chars(channel))
            save_conversation(app.state.store, settings, conv)
    elif held:
        text = with_waiting(text, held, Conversation(id="task"), settings.language)
    await app.state.delivery.deliver(title, text.strip())
    return wants_clean


def build_agent(
    settings: Settings,
    ha: HomeAssistantClient | None,
    http: httpx.AsyncClient,
    home: httpx.AsyncClient | None = None,
) -> Agent:
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    check_subscription(settings)
    # The Claude subscription (docs/subscription.md): it takes the turns it can, the API
    # the others; without an API key, NoApi stands there and says what needs one.
    keyless = settings.subscription_enabled and key is None
    llm: LLMClient = (
        NoApi() if keyless else build_llm_client(settings.llm_provider, key, settings.llm_base_url)
    )
    engine = subscription_engine(settings, settings.subscription_model or settings.llm_model)
    if engine is not None:
        log.info(
            "engine: Claude subscription (%s) for %s, %s",
            settings.subscription_model or settings.llm_model,
            "every display" if keyless else ", ".join(settings.subscription_displays),
            "no API key for the rest"
            if keyless
            else f"the API ({settings.llm_model}) for the rest",
        )
    kept = build_kept(settings, http)
    registry = build_registry(settings, ha, http, llm, home, kept)
    return Agent(
        llm,
        registry,
        prompt_config(settings),
        model=settings.llm_model,
        max_tokens=settings.max_tokens,
        max_tool_iterations=settings.max_tool_iterations,
        max_chat_tool_iterations=settings.max_chat_tool_iterations,
        max_chat_tokens=settings.max_chat_tokens,
        max_history_messages=settings.max_history_messages,
        memory=kept.prompt_text,
        journal=kept.journal,
        engine=engine,
        engine_displays=frozenset(settings.subscription_displays),
        api_fallback=not keyless,
    )


def _kept_name(conversation_id: str) -> str:
    """A conversation's name on disk: its id is the client's text, never a path."""
    return hashlib.sha256(conversation_id.encode()).hexdigest()[:32]


def conversation_file(settings: Settings, conversation_id: str) -> Path:
    return Path(settings.data_dir) / "conversations" / f"{_kept_name(conversation_id)}.json"


def file_store(settings: Settings, conversation_id: str) -> FileStore | None:
    """Where a chat conversation's files are kept across restarts."""
    if not settings.files_keep_days:
        return None
    directory = Path(settings.data_dir) / "files" / _kept_name(conversation_id)
    return FileStore(directory, settings.files_keep_days)


def keep_files(settings: Settings, conv: Conversation) -> None:
    store = file_store(settings, conv.id)
    if store is None or conv.file_store is not None:
        return
    try:
        conv.attach_store(store)
    except (OSError, KeyError, TypeError) as exc:
        log.warning("Could not read the kept files of a conversation: %s", exc)


def save_conversation(store: ConversationStore, settings: Settings, conv: Conversation) -> None:
    """A chat is answered hours later: a restart in between must not make it forget what
    was said, nor what waits for the user's go."""
    try:
        store.save(conv, conversation_file(settings, conv.id))
    except (OSError, TypeError, ValueError) as exc:
        log.warning("Could not save a conversation: %s", exc)


def restore_conversations(store: ConversationStore, settings: Settings) -> int:
    """Bring back the chat conversations saved before a restart, with their files; the
    ones past their lifetime are removed, as are the files past their days."""
    restored = 0
    for path in sorted((Path(settings.data_dir) / "conversations").glob("*.json")):
        try:
            conv = store.restore(path)
        except (KeyError, TypeError, ValueError):
            conv = None
        if conv is None:
            path.unlink(missing_ok=True)
            continue
        keep_files(settings, conv)
        restored += 1
    files = Path(settings.data_dir) / "files"
    for directory in files.iterdir() if files.is_dir() else ():
        if directory.is_dir():
            kept = FileStore(directory, settings.files_keep_days)
            try:
                empty = not kept.prune()
            except (KeyError, TypeError):
                empty = True
            if empty:
                kept.clear()
    return restored


def with_waiting(
    text: str,
    actions: list[dict],
    conv: Conversation,
    language: str = "en",
    option_chars: int | None = None,
) -> str:
    """Add, written by the code, every write done without a go and every action waiting
    for the user's go in the turn, so none goes unnoticed; only actions listed here can
    run on their yes. ``option_chars``: the reply goes to a chat that shows them as a
    choice to tick right below (chat_session.py), each option that long at most: only
    those too long for an option are written out here."""
    s = strings(language)
    lines = [
        s.written_line.format(summary=a["summary"]) for a in actions if a.get("type") == "written"
    ]
    for draft in (a for a in actions if a.get("type") == "draft"):
        names = ", ".join(draft["attachments"])
        files = s.draft_files.format(files=names) if names else ""
        lines.append(
            s.draft_line.format(to=", ".join(draft["to"]), subject=draft["subject"], files=files)
        )
        if draft.get("check"):
            lines.append(s.draft_check.format(files=", ".join(draft["check"])))
    proposals = [a for a in actions if a.get("type") == "proposal"]
    if proposals:
        header = s.waiting_header if option_chars is None else s.waiting_in_poll
        lines += ["", header] if lines else [header]
        lines += [
            f"{p['number']}. {p['summary']}"
            for p in proposals
            if option_chars is None or too_long_for_option(p["number"], p["summary"], option_chars)
        ]
        conv.mark_shown([p["number"] for p in proposals])
    return "\n".join([text, "", *lines]) if lines else text


def build_transcriber(settings: Settings) -> Transcriber | None:
    """Voice messages as text, when WHISPER_MODEL is set and the "voice" extra is there."""
    if not settings.whisper_model:
        return None
    if not voice_available():
        log.error('WHISPER_MODEL needs the faster-whisper package: pip install ".[voice]"')
        return None
    return shared_transcriber(
        settings.whisper_model,
        str(Path(settings.data_dir) / "whisper"),
        tuple(settings.whisper_languages or [settings.language]),
    )


@dataclass
class Channels:
    """The chat channels that are set up, and the loops that keep them listening."""

    channels: list[ChatChannel]
    loops: list[Coroutine[Any, Any, None]]


def build_channels(settings: Settings, http: httpx.AsyncClient) -> Channels:
    """One channel per chat the agent can be talked to in (docs/chat.md); none unless
    configured. A new messaging app is one more block here."""
    built = Channels([], [])
    if not settings.chat_enabled:
        return built
    transcriber = build_transcriber(settings)
    transcribe = transcriber.transcribe if transcriber else None
    if transcriber:
        built.loops.append(transcriber.warm_up())
    if settings.whatsapp_enabled:
        whatsapp = WhatsAppChannel(
            Waha(http, *settings.chat_waha),
            settings.whatsapp_chat_id,
            language=settings.language,
            marker=settings.whatsapp_marker,
            poll_seconds=settings.whatsapp_poll_seconds,
            transcribe=transcribe,
            own_number=settings.whatsapp_own_number,
        )
        built.channels.append(whatsapp)
        built.loops += whatsapp.tasks()
    if settings.telegram_enabled:
        api = TelegramApi(
            http, settings.telegram_bot_token.get_secret_value(), settings.telegram_api_url
        )
        bot = TelegramBot(
            api, settings.telegram_chat_ids, language=settings.language, transcribe=transcribe
        )
        built.loops.append(bot.run())
        for channel in bot.channels.values():
            built.channels.append(channel)
            built.loops += channel.tasks()
    return built


def chat_conversation(app: FastAPI, channel: str) -> Conversation:
    """A channel's conversation: kept across restarts, with its files and what waits."""
    settings: Settings = app.state.settings
    conv = app.state.store.get(f"chat-{channel}", settings.chat_conversation_ttl_seconds)
    conv.kept = True
    keep_files(settings, conv)
    return conv


def build_sessions(app: FastAPI, channels: list[ChatChannel]) -> dict[str, ChatSession]:
    settings: Settings = app.state.settings
    sessions = {}

    def save(conv: Conversation) -> None:
        app.state.store.touch(conv)
        save_conversation(app.state.store, settings, conv)

    for channel in channels:
        tracker = Tracker(
            channel.send, Path(settings.data_dir) / "run-durations.json", settings.language
        )
        session = ChatSession(
            channel,
            app.state.agent,
            conversation=lambda name=channel.name: chat_conversation(app, name),
            save=save,
            with_waiting=lambda text, actions, conv, option_chars=None: with_waiting(
                text, actions, conv, settings.language, option_chars
            ),
            language=settings.language,
            vote_grace_seconds=settings.chat_vote_grace_seconds,
            tracker=tracker,
        )
        channel.bind(session)
        sessions[channel.name] = session
    return sessions


def chat_session(request: Request, deliver: str) -> ChatSession | None:
    """The chat a request asks its reply to be sent in: None for "response"; "chat" is
    the first channel set up, anything else a channel's name."""
    if deliver == "response":
        return None
    sessions: dict[str, ChatSession] = request.app.state.chats
    session = next(iter(sessions.values()), None) if deliver == "chat" else sessions.get(deliver)
    if session is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"No chat channel is set up for {deliver!r}"
        )
    return session


async def answer(request: Request, conv: Conversation, body: AskRequest) -> tuple[str, list[dict]]:
    """One request of the user's, to its end: the reply with what waits listed under it,
    then each plan they just approved, run as a request of its own."""
    agent: Agent = request.app.state.agent
    language = request.app.state.settings.language
    files = [file.attachment() for file in body.files]
    try:
        reply = await agent.ask(
            conv, body.text.strip(), body.context, display=body.display, files=files or None
        )
        texts = [with_waiting(reply.text, reply.actions, conv, language)]
        actions = list(reply.actions)
        while conv.plans:
            # Clean, so it can act in a row, and carrying the plan, so the writes it spells
            # out run without another go. It is still not the user's own message: what only
            # they may ask for (needs_user_request) waits for them.
            plan = " ".join(conv.plans.pop(0).split())
            done = await agent.ask(
                conv,
                f"{PLAN_TAG} {plan}]",
                display=body.display,
                scheduled=True,
                approved_plan=plan,
            )
            texts.append(with_waiting(done.text, done.actions, conv, language))
            actions += done.actions
    except BaseException:
        # A turn that failed never showed its writes: they must not run on a later yes.
        conv.drop_unshown()
        conv.plans.clear()
        raise
    return "\n\n".join(texts), actions


def log_setup(settings: Settings, tools: set[str], running: dict[str, bool]) -> None:
    """Two lines at startup, what is on and what is off, for whoever reads the log to see
    why the agent does or doesn't do something. ``tools``: the tools that were built, which
    say more than the settings (a library that is missing, a key that can't be used);
    ``running``: what only the started app knows (a job that needs a way to reach the
    user)."""

    def built(tool: str, wanted: object) -> bool:
        return tool in tools if tools else bool(wanted)

    devices = has_home_devices(settings) or bool(
        settings.nuki_api_token
        or settings.netatmo_client_id
        or settings.petkit_username
        or settings.android_tv_host
        or settings.apple_devices.strip()
    )
    parts = {
        f"model ({settings.llm_provider}, {settings.llm_model})": True,
        "Claude subscription": settings.subscription_enabled,
        f"web search ({settings.web_search_backend})": built(
            "web_search", settings.web_search_enabled
        ),
        "web pages": built("open_web_page", settings.web_pages_enabled),
        "investigations": built("domain_lookup", settings.investigations_enabled),
        "remote browser": built("browser_open", settings.browser_cdp_host),
        f"memory ({settings.memory_store})": built("remember", settings.memory_store != "off"),
        f"journal ({settings.journal_keep_days} days)": settings.journal_keep_days > 0,
        "calendar": built("get_calendar_events", settings.caldav_url),
        f"mail ({len(settings.mail_accounts)})": bool(settings.mail_accounts),
        "documents": built("list_saved_files", settings.documents_enabled),
        f"drives ({len(settings.drives)})": settings.drives_enabled,
        f"connectors ({len(settings.mcp_servers)})": bool(settings.mcp_servers),
        "knowledge base": bool(settings.kb_url and settings.kb_token),
        "GitHub": settings.github_token is not None,
        "iCloud apps": built("get_reminders", settings.icloud_apple_id),
        "home devices": devices,
        **running,
    }
    log.info("on: %s", ", ".join(name for name, on in parts.items() if on))
    log.info("off: %s", ", ".join(name.split(" (")[0] for name, on in parts.items() if not on))


class ConfigError(ValueError):
    """The settings can't be used; the message says which and why, never their values."""


def load_settings() -> Settings:
    """The settings, or one line per mistake. pydantic's own message quotes what it was
    given, which for a rule on several settings is the start of the first of them, the API
    token: none of that belongs in a log."""
    try:
        return get_settings()
    except ValidationError as exc:
        lines = [
            f"- {'.'.join(str(part) for part in error['loc']).upper() or 'settings'}: "
            f"{error['msg'].removeprefix('Value error, ')}"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        raise ConfigError("The settings (.env) can't be used:\n" + "\n".join(lines)) from None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    ha = None
    if settings.ha_token:
        ha = HomeAssistantClient(
            settings.ha_url,
            settings.ha_token.get_secret_value(),
            settings.ha_timeout_seconds,
            proxy=settings.ha_proxy_url,
        )
    http = httpx.AsyncClient(timeout=settings.open_meteo_timeout_seconds)
    app.state.settings = settings
    app.state.ha = ha
    app.state.http = http
    home = home_network_client(settings) if has_home_devices(settings) else None
    await load_crypt_patterns(settings, http)
    app.state.agent = build_agent(settings, ha, http, home)
    app.state.store = ConversationStore(settings.conversation_ttl_seconds)
    try:
        if restored := restore_conversations(app.state.store, settings):
            log.info("%d chat conversation(s) restored", restored)
    except OSError as exc:
        log.warning("Could not restore the saved conversations: %s", exc)
    app.state.ntfy = None
    if settings.ntfy_url:
        token = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
        app.state.ntfy = Ntfy(http, settings.ntfy_url, token)
    # The chat channels (docs/chat.md): each listens for the user's messages, and is where
    # what the agent says by itself goes first.
    built = build_channels(settings, http)
    app.state.chats = build_sessions(app, built.channels)
    listening = [asyncio.create_task(loop) for loop in built.loops]
    listening += [asyncio.create_task(s.tracker.run()) for s in app.state.chats.values()]
    if app.state.chats:
        log.info("chat channels: %s", ", ".join(app.state.chats))
    app.state.delivery = build_delivery(settings, app.state.ntfy, app.state.chats)
    # What the tools keep doing in the background (a drive's index kept fresh), and the
    # tasks the agent starts by itself.
    registry = getattr(app.state.agent, "tools", None)
    background = list(getattr(registry, "jobs", []))
    if (runner := build_task_runner(app, http)) is not None:
        background.append(runner.run)
    # Coding jobs, the coder's permission prompts and the nightly review (docs/jobs.md).
    app.state.coder_approvals = None
    job_runner = start_jobs(app, http, background)
    jobs = [asyncio.create_task(job()) for job in background]
    # Home devices: the departure check and the pet supplies watch (docs/home-devices.md).
    devices = getattr(registry, "devices", {})
    app.state.departure = None
    if settings.departure_check and isinstance(registry, ToolRegistry):
        check = Departure(registry, language=settings.language, tv_name=settings.android_tv_name)
        app.state.departure = check if check.watches_something else None
    if (alerts := getattr(registry, "activity_alerts", None)) is not None:
        alert_title = strings(settings.language).activity_alert_title

        async def send_alert(text: str) -> bool:
            try:
                await tell_user(app, alert_title, text)
            except (NotifyError, httpx.HTTPError) as exc:
                # Not said: the alert is tried again on the next round.
                log.warning("Activity alert not sent: %s", exc)
                return False
            return True

        jobs.append(asyncio.create_task(alerts.watch(send_alert)))
    if (pets := build_pet_alerts(settings, devices)) is not None:
        title = strings(settings.language).pet_alert_title
        jobs.append(asyncio.create_task(pets.watch(lambda text: tell_user(app, title, text))))
    log_setup(
        settings,
        registry.names() if isinstance(registry, ToolRegistry) else set(),
        {
            "Home Assistant": ha is not None,
            "tasks the agent runs by itself": runner is not None,
            "coding jobs": job_runner is not None,
            "chat (" + ", ".join(app.state.chats) + ")" if app.state.chats else "chat": bool(
                app.state.chats
            ),
            "push notifications (ntfy)": app.state.ntfy is not None,
            "departure check": app.state.departure is not None,
            "activity alerts": alerts is not None,
            "pet supplies alerts": pets is not None,
            "nightly review": job_runner is not None and bool(settings.review_repo),
        },
    )
    try:
        yield
    finally:
        # First, while the channels can still say an answer was cut short.
        for task in listening:
            task.cancel()
        await asyncio.gather(*listening, return_exceptions=True)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if job_runner is not None:
            await job_runner.aclose()
        if "petkit" in devices:
            await devices["petkit"].aclose()
        if ha is not None:
            await ha.aclose()
        if home is not None:
            await home.aclose()
        await http.aclose()


app = FastAPI(title="intelligent-home-agent", version=__version__, lifespan=lifespan)
_bearer = HTTPBearer(auto_error=False)


@app.exception_handler(LLMError)
async def model_failed(request: Request, exc: LLMError) -> JSONResponse:
    """The model's server is down or refused the request: said in one line, to the client
    and in the log, instead of a crash."""
    log.error("The model could not answer: %s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_502_BAD_GATEWAY)


def require_token(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> None:
    expected = request.app.state.settings.agent_api_token.get_secret_value()
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")


def require_coder_token(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> None:
    """The coder's own token: the one the agent uses to reach it, used the other way."""
    expected = request.app.state.settings.coder_token
    if (
        expected is None
        or creds is None
        or not secrets.compare_digest(creds.credentials, expected.get_secret_value())
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")


@app.post("/coder/permission", dependencies=[Depends(require_coder_token)])
async def coder_permission(request: Request) -> dict:
    """A permission prompt of the coder's Claude Code, asked to the user (coder_approvals);
    {} leaves it to the Claude app."""
    approvals: Approvals | None = getattr(request.app.state, "coder_approvals", None)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if approvals is None or not isinstance(body, dict):
        return {}
    return await approvals.ask(body)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/s/{token}/{name}", include_in_schema=False)
async def shared_image(token: str, name: str) -> Response:
    """A picture put online for a reverse image search, while its link lasts: no bearer
    token, as the search engines fetch it; the address itself is the secret."""
    shared = LINKS.get(token)
    if shared is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(
        shared.data,
        media_type=shared.mimetype,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, noarchive"},
    )


@app.get("/health/ha", dependencies=[Depends(require_token)])
async def health_ha(request: Request) -> dict[str, bool]:
    ha = request.app.state.ha
    return {"home_assistant": ha is not None and await ha.ping()}


@app.post(
    "/v1/ask",
    response_model=AskResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def ask(body: AskRequest, request: Request) -> AskResponse:
    store: ConversationStore = request.app.state.store
    settings: Settings = request.app.state.settings
    if (chat := chat_session(request, body.deliver)) is not None:
        # Asked from elsewhere (a voice shortcut), answered in the chat too, where the
        # user can go on: what waits for their go is shown there, as a choice.
        files = [file.attachment() for file in body.files]
        reply = await chat.request(body.text.strip(), body.context, files or None)
        return AskResponse(reply=reply, conversation_id=chat.conversation().id)
    # A chat can be answered hours later; a voice exchange is over in minutes.
    ttl = settings.chat_conversation_ttl_seconds if body.display == "chat" else None
    conv = store.get(body.conversation_id, ttl)
    # A chat outlives restarts, with its files and what waits for a go (DATA_DIR); once
    # kept, a conversation stays so whatever the display of its next requests.
    kept = body.display == "chat" or conv.kept
    async with conv.lock:
        if kept:
            conv.kept = True
            keep_files(settings, conv)
        text, actions = await answer(request, conv, body)
        store.touch(conv)
        if kept:
            save_conversation(store, settings, conv)
        shared = [
            FileOut(name=file.filename, mimetype=file.mimetype)
            for a in actions
            if a.get("type") == "share_file" and (file := conv.files.get(a.get("name", "")))
        ]
    return AskResponse(
        reply=text,
        conversation_id=conv.id,
        # What the client does on the phone; what waits for a go is in the reply itself.
        actions=[a for a in actions if a.get("type") == "reminder"],
        files=shared or None,
        follow_up=None if closes_conversation(body.text, *_closing(request)) else True,
    )


@app.get("/v1/files/{name}", dependencies=[Depends(require_token)])
async def get_file(
    name: str, request: Request, conversation_id: str = DEFAULT_CONVERSATION
) -> Response:
    """A file of the conversation (one the agent made, fetched, or was sent), by name."""
    store: ConversationStore = request.app.state.store
    conv = store.peek(conversation_id)
    file = conv.files.get(name) if conv else None
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file in this conversation")
    return Response(
        content=file.data,
        media_type=file.mimetype,
        headers={"Content-Disposition": "attachment"},
    )


def _closing(request: Request) -> tuple[str, str]:
    settings = request.app.state.settings
    return settings.language, settings.assistant_name


@app.post(
    "/v1/briefing",
    response_model=BriefingResponse,
    dependencies=[Depends(require_token)],
)
async def briefing(body: BriefingRequest, request: Request) -> BriefingResponse:
    """The morning briefing or evening recap, written to be read, for schedulers.

    Each call starts from a fresh conversation, so a scheduled briefing never sees (or
    leaves behind) another conversation's history.
    """
    settings = request.app.state.settings
    s = strings(settings.language)
    text = s.briefing_request if body.kind == "morning" else s.recap_request
    ntfy: Ntfy | None = request.app.state.ntfy
    if body.push and ntfy is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "push needs NTFY_URL")
    if (chat := chat_session(request, body.deliver)) is not None:
        # In the chat the briefing can be answered: it joins that conversation, and
        # what it would have the user approve waits there for their go.
        reply = await chat.request(text, body.context, scheduled=True)
        return BriefingResponse(reply=reply, delivered=True)
    conv = Conversation(id=f"briefing-{body.kind}")
    # Nobody answers a scheduler's briefing, and its conversation ends with it: nothing
    # waits for a go there.
    reply = await request.app.state.agent.ask(
        conv, text, body.context, display="notification", scheduled=True, answerable=False
    )
    pushed = False
    if body.push and ntfy is not None:
        title = s.morning_briefing_title if body.kind == "morning" else s.evening_recap_title
        try:
            await ntfy.send(f"{settings.assistant_name} · {title}", reply.text)
            pushed = True
        except NotifyError as exc:
            log.warning("briefing push failed: %s", exc)
    reminders = [a for a in reply.actions if a.get("type") == "reminder"]
    return BriefingResponse(reply=reply.text, actions=reminders, pushed=pushed)


@app.get("/v1/tasks/reports", dependencies=[Depends(require_token)])
async def task_reports(request: Request, since: str | None = None) -> dict[str, list]:
    """What the tasks the agent ran by itself reported, the last ones, oldest first; with
    ``since`` (the ``at`` of the last one already fetched), only the later ones."""
    outbox = request.app.state.delivery.outbox
    return {"reports": outbox.since(since) if outbox else []}


@app.post(
    "/v1/departure",
    response_model=DepartureResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_token)],
)
async def left_home(body: DepartureRequest, request: Request) -> DepartureResponse:
    """The user left home (a phone automation calls this): what is still unlocked, on or
    playing, written by the code, each fix waiting for their yes in the conversation."""
    check: Departure | None = request.app.state.departure
    if check is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Departure check is off")
    settings: Settings = request.app.state.settings
    ntfy: Ntfy | None = request.app.state.ntfy
    if body.push and ntfy is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "push needs NTFY_URL")
    store: ConversationStore = request.app.state.store
    chat = chat_session(request, body.deliver)
    conv = chat.conversation() if chat is not None else store.get(body.conversation_id)
    option_chars = chat.channel.option_chars if chat is not None else None
    async with conv.lock:
        report = await check.check(conv)
        if not report.text:
            return DepartureResponse(reply="", conversation_id=conv.id)
        text = with_waiting(report.text, report.actions, conv, settings.language, option_chars)
        record_departure(conv, text)
        # The answer may come a while after leaving: kept as long as a chat, and
        # across a restart like one.
        conv.ttl_seconds = settings.chat_conversation_ttl_seconds
        conv.kept = True
        store.touch(conv)
        save_conversation(store, settings, conv)
    if chat is not None:
        try:
            await chat.channel.send(text)
            await chat.after_reply()
        except httpx.HTTPError as exc:
            log.warning("Could not say the departure check in the chat: %s", exc)
    pushed = False
    if body.push and ntfy is not None:
        title = strings(settings.language).departure_title
        try:
            await ntfy.send(f"{settings.assistant_name} · {title}", text)
            pushed = True
        except NotifyError as exc:
            log.warning("departure push failed: %s", exc)
    return DepartureResponse(
        reply=text,
        conversation_id=conv.id,
        pushed=pushed,
        follow_up=True if report.actions else None,
    )


@app.post("/v1/reset", status_code=204, dependencies=[Depends(require_token)])
async def reset(request: Request, conversation_id: str = DEFAULT_CONVERSATION) -> None:
    """Forget a conversation: what was said, what waits for a go, and its kept files."""
    settings: Settings = request.app.state.settings
    request.app.state.store.reset(conversation_id)
    conversation_file(settings, conversation_id).unlink(missing_ok=True)
    if (store := file_store(settings, conversation_id)) is not None:
        store.clear()
