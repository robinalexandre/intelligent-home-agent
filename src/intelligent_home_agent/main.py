"""HTTP API: what the Siri Shortcut, Home Assistant or any other client talks to."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from intelligent_home_agent import __version__
from intelligent_home_agent.activities import CATALOG
from intelligent_home_agent.agent import Agent
from intelligent_home_agent.config import Settings, get_settings
from intelligent_home_agent.conversations import Attachment, Conversation, ConversationStore
from intelligent_home_agent.ha_client import HomeAssistantClient
from intelligent_home_agent.home_devices import (
    build_home_device_tools,
    has_home_devices,
    home_network_client,
)
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.journal import Journal
from intelligent_home_agent.llm import ANTHROPIC, LLMClient, build_llm_client
from intelligent_home_agent.notify import NotifyError, Ntfy
from intelligent_home_agent.prompts import PromptConfig
from intelligent_home_agent.tools import (
    CalDavCalendar,
    EntityPolicy,
    GitHub,
    ImapMailbox,
    KnowledgeBase,
    OpenMeteo,
    ReminderRepo,
    Router,
    Tool,
    ToolRegistry,
    build_assistant_reminder_tools,
    build_calendar_tools,
    build_email_tools,
    build_engine_web_tools,
    build_general_tools,
    build_github_tools,
    build_home_assistant_tools,
    build_knowledge_tools,
    build_outdoor_tools,
    build_search_engine,
    build_surf_tools,
    build_travel_tools,
    build_weather_tools,
    build_web_tools,
    build_wind_tools,
)
from intelligent_home_agent.tools.code_runner import build_code_tools
from intelligent_home_agent.tools.connectors import Connector, build_connector_tools
from intelligent_home_agent.tools.documents import FONT_DIR, build_document_tools, paper_for
from intelligent_home_agent.tools.generation import (
    OPENAI_URL,
    Gemini,
    OpenAI,
    Provider,
    build_generation_tools,
)
from intelligent_home_agent.tools.illustrations import build_illustration_tools
from intelligent_home_agent.tools.long_results import build_long_result_tools
from intelligent_home_agent.tools.mcp_client import (
    MCPClient,
    NoAuth,
    OAuthToken,
    StaticToken,
    TokenSource,
)
from intelligent_home_agent.tools.pdf_forms import build_pdf_form_tools
from intelligent_home_agent.tools.pdf_pages import build_pdf_page_tools
from intelligent_home_agent.tools.proposals import build_proposal_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_CONVERSATION = "default"
MAX_FILES = 5
# A PDF of 20 MB, the most the agent reads, once in base64.
MAX_FILE_BASE64_CHARS = 28_000_000
# How a plan the user approved comes back to the agent, as a request of its own.
PLAN_TAG = "[Plan you proposed and the user approved as shown; do it now as their request:"


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


class BriefingResponse(BaseModel):
    reply: str
    actions: list[dict] = []
    pushed: bool = False


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
) -> ToolRegistry:
    policy = EntityPolicy.from_lists(
        settings.ha_exposed_domains, settings.ha_sensitive_domains, settings.ha_entity_denylist
    )
    meteo = OpenMeteo(http, settings.timezone)
    home = None
    if settings.home_latitude is not None and settings.home_longitude is not None:
        home = (settings.home_latitude, settings.home_longitude)

    registry = ToolRegistry()
    registry.extend(build_general_tools(settings.timezone))
    if ha is not None:
        registry.extend(build_home_assistant_tools(ha, policy))
    registry.extend(build_home_device_tools(settings, home))
    registry.extend(build_weather_tools(meteo, home=home, language=settings.language))
    registry.extend(build_wind_tools(meteo, settings.wind_spots, model=settings.wind_model))
    registry.extend(build_surf_tools(meteo, settings.surf_spots, model=settings.wind_model))
    activities = [CATALOG[key] for key in settings.outdoor_activities]
    registry.extend(build_outdoor_tools(meteo, activities, home=home, language=settings.language))
    if settings.caldav_url and settings.caldav_username and settings.caldav_password:
        calendar = CalDavCalendar(
            settings.caldav_url,
            settings.caldav_username,
            settings.caldav_password.get_secret_value(),
            settings.caldav_calendars,
        )
        registry.extend(build_calendar_tools(calendar, settings.timezone))
    mailboxes = [ImapMailbox(account) for account in settings.mail_accounts]
    registry.extend(build_email_tools(mailboxes, settings.timezone))
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
    if settings.github_token:
        token = settings.github_token.get_secret_value()
        registry.extend(build_github_tools(GitHub(http, token)))
        if settings.assistant_reminders_repo:
            reminders = ReminderRepo(
                http, token, settings.assistant_reminders_repo, settings.assistant_reminders_path
            )
            registry.extend(build_assistant_reminder_tools(reminders, settings.timezone))
    registry.extend(build_connector_tools(build_connectors(settings, http)))
    if settings.kb_url and settings.kb_token:
        kb = KnowledgeBase(http, settings.kb_url, settings.kb_token.get_secret_value())
        registry.extend(build_knowledge_tools(kb))
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
                llm,
                engine,
                model=settings.web_search_model,
                max_results=settings.web_search_results,
                language=settings.language,
            )
        )
    registry.extend(build_document_side_tools(settings, http, llm))
    # The core's own tools, last: the writes waiting for the user's go (the registry parks
    # them whatever is configured) and the long results read part by part.
    registry.extend(build_proposal_tools(registry, language=settings.language))
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
    )


def build_journal(settings: Settings) -> Journal | None:
    if not settings.journal_keep_days:
        return None
    directory = Path(settings.data_dir) / "journal"
    return Journal(directory, settings.timezone, settings.journal_keep_days)


def build_agent(
    settings: Settings,
    ha: HomeAssistantClient | None,
    http: httpx.AsyncClient,
    home: httpx.AsyncClient | None = None,
) -> Agent:
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    llm = build_llm_client(settings.llm_provider, key, settings.llm_base_url)
    registry = build_registry(settings, ha, http, llm, home)
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
        journal=build_journal(settings),
    )


def with_waiting(text: str, actions: list[dict], conv: Conversation, language: str = "en") -> str:
    """Add, written by the code, every write done without a go and every action waiting
    for the user's go in the turn, so none goes unnoticed; only actions listed here can
    run on their yes."""
    s = strings(language)
    lines = [
        s.written_line.format(summary=a["summary"]) for a in actions if a.get("type") == "written"
    ]
    proposals = [a for a in actions if a.get("type") == "proposal"]
    if proposals:
        lines += ["", s.waiting_header] if lines else [s.waiting_header]
        lines += [f"{p['number']}. {p['summary']}" for p in proposals]
        conv.mark_shown([p["number"] for p in proposals])
    return "\n".join([text, "", *lines]) if lines else text


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
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
    home = home_network_client(settings) if has_home_devices(settings) else None
    app.state.agent = build_agent(settings, ha, http, home)
    app.state.store = ConversationStore(settings.conversation_ttl_seconds)
    app.state.ntfy = None
    if settings.ntfy_url:
        token = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
        app.state.ntfy = Ntfy(http, settings.ntfy_url, token)
    try:
        yield
    finally:
        if ha is not None:
            await ha.aclose()
        if home is not None:
            await home.aclose()
        await http.aclose()


app = FastAPI(title="intelligent-home-agent", version=__version__, lifespan=lifespan)
_bearer = HTTPBearer(auto_error=False)


def require_token(
    request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> None:
    expected = request.app.state.settings.agent_api_token.get_secret_value()
    if creds is None or not secrets.compare_digest(creds.credentials, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


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
    # A chat can be answered hours later; a voice exchange is over in minutes.
    ttl = settings.chat_conversation_ttl_seconds if body.display == "chat" else None
    conv = store.get(body.conversation_id, ttl)
    async with conv.lock:
        text, actions = await answer(request, conv, body)
        store.touch(conv)
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
    conv = Conversation(id=f"briefing-{body.kind}")
    # Nobody answers a scheduler's briefing, and its conversation ends with it: nothing
    # waits for a go there.
    reply = await request.app.state.agent.ask(
        conv, text, body.context, display="notification", scheduled=True, answerable=False
    )
    pushed = False
    if body.push and ntfy is not None:
        title = "Briefing" if body.kind == "morning" else "Recap"
        try:
            await ntfy.send(f"{settings.assistant_name} · {title}", reply.text)
            pushed = True
        except NotifyError as exc:
            log.warning("briefing push failed: %s", exc)
    reminders = [a for a in reply.actions if a.get("type") == "reminder"]
    return BriefingResponse(reply=reply.text, actions=reminders, pushed=pushed)


@app.post("/v1/reset", status_code=204, dependencies=[Depends(require_token)])
async def reset(request: Request, conversation_id: str = DEFAULT_CONVERSATION) -> None:
    request.app.state.store.reset(conversation_id)
