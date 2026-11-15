"""HTTP API: what the Siri Shortcut, Home Assistant or any other client talks to."""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
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
from intelligent_home_agent.config_crypt import ConfigCrypt, CryptError, patterns_from_attributes
from intelligent_home_agent.config_crypt import available as crypt_available
from intelligent_home_agent.conversations import (
    Attachment,
    Conversation,
    ConversationStore,
    FileStore,
)
from intelligent_home_agent.ha_client import HomeAssistantClient
from intelligent_home_agent.home_devices import (
    build_home_device_tools,
    has_home_devices,
    home_network_client,
)
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.journal import Journal
from intelligent_home_agent.llm import LLMClient, build_llm_client
from intelligent_home_agent.notify import NotifyError, Ntfy
from intelligent_home_agent.prompts import PromptConfig
from intelligent_home_agent.shared_links import LINKS
from intelligent_home_agent.tools import (
    CalDavCalendar,
    CardDavBirthdays,
    EntityPolicy,
    GitHub,
    GitHubFile,
    ImapMailbox,
    Investigator,
    KnowledgeBase,
    LocalFile,
    Memory,
    MergedCalendar,
    OpenMeteo,
    ReminderRepo,
    Router,
    ToolRegistry,
    TurnedDown,
    WebPages,
    build_assistant_reminder_tools,
    build_calendar_tools,
    build_calendar_write_tools,
    build_draft_tools,
    build_email_tools,
    build_engine_web_tools,
    build_general_tools,
    build_github_tools,
    build_home_assistant_tools,
    build_knowledge_tools,
    build_memory_tools,
    build_osint_tools,
    build_outdoor_tools,
    build_past_conversation_tools,
    build_procedure_tools,
    build_search_engine,
    build_surf_tools,
    build_travel_tools,
    build_turned_down_tools,
    build_weather_tools,
    build_web_page_tools,
    build_web_tools,
    build_wind_tools,
    trusted_domains,
)
from intelligent_home_agent.tools.connectors import Connector, build_connector_tools
from intelligent_home_agent.tools.github import API_URL
from intelligent_home_agent.tools.long_results import build_long_result_tools
from intelligent_home_agent.tools.mcp_client import (
    MCPClient,
    NoAuth,
    OAuthToken,
    StaticToken,
    TokenSource,
)
from intelligent_home_agent.tools.memory import folder_name
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
    kept: Kept | None = None,
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
    if settings.investigations_enabled:
        investigator = Investigator(http, nominatim_url=settings.nominatim_url)
        registry.extend(build_osint_tools(investigator, settings.public_url))
    # The core's own tools, last: the writes waiting for the user's go (the registry parks
    # them whatever is configured) and the long results read part by part.
    registry.extend(build_proposal_tools(registry, language=settings.language))
    registry.extend(build_long_result_tools())
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


def build_crypt(settings: Settings) -> ConfigCrypt | None:
    """The memory repository's git-crypt codec, when it has a key; its perimeter is read
    from the repository's .gitattributes at startup (load_crypt_patterns)."""
    if settings.memory_store != "github" or not settings.memory_crypt_key:
        return None
    # Until the repository's .gitattributes is read, the agent's own folders count as
    # encrypted: a write made before that, or after a failed read, is never in clear by
    # default.
    own = dict.fromkeys(
        f"{path.rsplit('/', 1)[0]}/**" if "/" in path else path for path in memory_paths(settings)
    )
    return _crypt_for(settings.memory_crypt_key.get_secret_value(), tuple(own))


@lru_cache
def _crypt_for(key: str, own: tuple[str, ...]) -> ConfigCrypt | None:
    if not crypt_available():
        log.error('MEMORY_CRYPT_KEY needs the cryptography package: pip install ".[crypt]"')
        return None
    try:
        return ConfigCrypt.from_base64(key, list(own))
    except CryptError as exc:
        log.error("MEMORY_CRYPT_KEY is not a git-crypt key (%s)", exc)
        return None


async def load_crypt_patterns(settings: Settings, http: httpx.AsyncClient) -> None:
    crypt = build_crypt(settings)
    if crypt is None or not settings.github_token:
        return
    try:
        resp = await http.get(
            f"{API_URL}/repos/{settings.memory_repo}/contents/.gitattributes",
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("could not read the memory repository's .gitattributes (%s)", exc)
        return
    patterns = patterns_from_attributes(resp.text)
    if patterns:
        crypt.patterns = patterns
    log.info("encrypted folders of the memory repository: %s", ", ".join(crypt.patterns))


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


def build_agent(
    settings: Settings,
    ha: HomeAssistantClient | None,
    http: httpx.AsyncClient,
    home: httpx.AsyncClient | None = None,
) -> Agent:
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    llm = build_llm_client(settings.llm_provider, key, settings.llm_base_url)
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


def with_waiting(text: str, actions: list[dict], conv: Conversation, language: str = "en") -> str:
    """Add, written by the code, every write done without a go and every action waiting
    for the user's go in the turn, so none goes unnoticed; only actions listed here can
    run on their yes."""
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
    """Forget a conversation: what was said, what waits for a go, and its kept files."""
    settings: Settings = request.app.state.settings
    request.app.state.store.reset(conversation_id)
    conversation_file(settings, conversation_id).unlink(missing_ok=True)
    if (store := file_store(settings, conversation_id)) is not None:
        store.clear()
