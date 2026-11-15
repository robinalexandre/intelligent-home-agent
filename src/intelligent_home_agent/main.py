"""HTTP API: what the Siri Shortcut, Home Assistant or any other client talks to."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from intelligent_home_agent import __version__
from intelligent_home_agent.activities import CATALOG
from intelligent_home_agent.agent import Agent
from intelligent_home_agent.config import Settings, get_settings
from intelligent_home_agent.conversations import Conversation, ConversationStore
from intelligent_home_agent.ha_client import HomeAssistantClient
from intelligent_home_agent.home_devices import (
    build_home_device_tools,
    has_home_devices,
    home_network_client,
)
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.llm import LLMClient, build_llm_client
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
from intelligent_home_agent.tools.connectors import Connector, build_connector_tools
from intelligent_home_agent.tools.mcp_client import (
    MCPClient,
    NoAuth,
    OAuthToken,
    StaticToken,
    TokenSource,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_CONVERSATION = "default"


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: str = Field(default=DEFAULT_CONVERSATION, max_length=64)
    # Extra data from the phone, e.g. today's reminders for the morning briefing.
    context: str | None = Field(default=None, max_length=4000)
    # "notification" when the reply is read on screen (briefing, recap), not spoken.
    display: Literal["voice", "notification"] = "voice"


class AskResponse(BaseModel):
    reply: str
    conversation_id: str
    # Things the Shortcut must do on the phone, e.g. {"type": "reminder", "title": ...}.
    actions: list[dict] = []
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
            )
        )
    log.info("tools: %s", ", ".join(d["name"] for d in registry.definitions()))
    return registry


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
    )


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
        max_history_messages=settings.max_history_messages,
    )


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
    conv = store.get(body.conversation_id)
    async with conv.lock:
        reply = await request.app.state.agent.ask(
            conv, body.text.strip(), body.context, notification=body.display == "notification"
        )
        store.touch(conv)
    return AskResponse(
        reply=reply.text,
        conversation_id=conv.id,
        actions=reply.actions,
        follow_up=None if closes_conversation(body.text, *_closing(request)) else True,
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
    reply = await request.app.state.agent.ask(conv, text, body.context, notification=True)
    pushed = False
    if body.push and ntfy is not None:
        title = "Briefing" if body.kind == "morning" else "Recap"
        try:
            await ntfy.send(f"{settings.assistant_name} · {title}", reply.text)
            pushed = True
        except NotifyError as exc:
            log.warning("briefing push failed: %s", exc)
    return BriefingResponse(reply=reply.text, actions=reply.actions, pushed=pushed)


@app.post("/v1/reset", status_code=204, dependencies=[Depends(require_token)])
async def reset(request: Request, conversation_id: str = DEFAULT_CONVERSATION) -> None:
    request.app.state.store.reset(conversation_id)
