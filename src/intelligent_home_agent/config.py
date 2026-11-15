"""Runtime configuration, loaded from environment variables (or a .env file).

Run ``python scripts/setup.py`` to write a .env interactively; .env.example lists every
setting with a comment.
"""

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from intelligent_home_agent.activities import resolve
from intelligent_home_agent.llm import (
    ANTHROPIC,
    CUSTOM,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_ANTHROPIC_SEARCH_MODEL,
    DOCUMENT_PROVIDERS,
    PROVIDERS,
    WEB_SEARCH_PROVIDERS,
    needs_key,
)

CsvList = Annotated[list[str], NoDecode]

# Sections the morning briefing and the evening recap can include.
BRIEF_SECTIONS = (
    "calendar",
    "travel",
    "mail",
    "pull_requests",
    "reminders",
    "weather",
    "activities",
)


class WindSpot(BaseModel):
    name: str
    latitude: float
    longitude: float
    # Compass points the spot works with (e.g. ["W", "SW", "NW"]); empty means any direction.
    directions: list[str] = []
    min_knots: float = 12


class SurfSpot(BaseModel):
    name: str
    latitude: float
    longitude: float
    # Compass point the beach faces (towards the sea): offshore wind comes from the opposite.
    facing: str
    min_wave_m: float = 0.5
    max_wave_m: float = 2.5


class MailAccount(BaseModel):
    name: str
    host: str
    username: str
    password: SecretStr
    port: int = 993
    mailbox: str = "INBOX"
    # Let the agent save drafts, flag emails and file them into the user's own folders in
    # this account. It never sends and never deletes. Off: the account is only read.
    write: bool = False
    # The From address of the drafts, when the login isn't the address (some providers
    # log in with a user name).
    address: str | None = None
    # The special folders, when the server neither marks them (SPECIAL-USE) nor gives them
    # a usual name: their exact names, e.g. "INBOX.Brouillons". Empty: found by itself.
    drafts: str | None = None
    sent: str | None = None
    trash: str | None = None


class McpServer(BaseModel):
    """A remote MCP server the agent may use (docs/connectors.md)."""

    # Short name in the agent's tool names: "bank" gives bank_help, bank_read, bank_write.
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    # What the user calls it, e.g. the bank's name; the name when empty.
    label: str = ""
    url: str
    # How to log in: a fixed token; OAuth client credentials (client_id, client_secret,
    # token_url); or an OAuth refresh token from scripts/mcp_login.py (client_id,
    # refresh_token, token_url). Nothing set: no login.
    token: SecretStr | None = None
    token_url: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = None
    resource: str | None = None
    scope: str | None = None
    # off: the agent only reads. confirm: a write waits for the user's yes, shown whole.
    writes: Literal["off", "confirm"] = "off"
    # Server tools that only read, though the server doesn't flag them read-only.
    read_tools: list[str] = []

    @model_validator(mode="after")
    def _login_is_whole(self) -> "McpServer":
        if not self.url.startswith(("https://", "http://")):
            raise ValueError(f"{self.name}: url must start with https:// or http://")
        if self.token is None and (self.client_id or self.refresh_token or self.client_secret):
            if not (self.client_id and self.token_url):
                raise ValueError(f"{self.name}: an OAuth login needs client_id and token_url")
            if self.refresh_token is None and self.client_secret is None:
                raise ValueError(f"{self.name}: set client_secret or refresh_token")
        return self


class Drive(BaseModel):
    """A file drive: one remote of the rclone sidecar (docs/drives.md)."""

    # Short name the tools take, e.g. "gdrive", "nas".
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    # What the user calls it ("Google Drive", "Home NAS"); the service's name when empty.
    label: str = ""
    # The remote as rclone's config names it, with an optional folder: "gdrive:",
    # "nas:Documents", or a view made with rclone's combine or alias.
    remote: str = Field(pattern=r"^[\w][\w .+@-]*:[^\x00-\x1f]*$")
    # rclone's type of the storage behind it (drive, iclouddrive, onedrive, dropbox,
    # webdav, sftp, s3...): only used to say what to do when its login fails, and where
    # the service keeps what is deleted. Empty: general advice.
    backend: str = ""
    # The agent only lists, searches and reads there.
    read_only: bool = False
    # Index the text inside its files for search_drive_content (every readable file is
    # downloaded once): false for a drive too big or billed by the download.
    content: bool = True


class Settings(BaseSettings):
    # Empty values (HOME_LATITUDE=) mean "not set", as in .env.example.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, env_ignore_empty=True
    )

    # --- API exposed to clients (Siri Shortcut, Home Assistant, scheduled tasks...) ----
    agent_api_token: SecretStr = Field(description="Bearer token every client must send.")

    # --- Language model ----------------------------------------------------------------
    # anthropic, one of the Chat Completions endpoints known by name (openai, mistral, groq,
    # openrouter, gemini, ollama, lmstudio), or openai-compatible with LLM_BASE_URL.
    llm_provider: str = ANTHROPIC
    # Not needed by a local server (ollama, lmstudio, openai-compatible).
    llm_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("llm_api_key", "anthropic_api_key")
    )
    # Required except for anthropic, where it defaults to DEFAULT_ANTHROPIC_MODEL.
    llm_model: str = Field("", validation_alias=AliasChoices("llm_model", "anthropic_model"))
    # Where the Chat Completions API is, ending in /v1 for most servers. Required for
    # openai-compatible; for a named provider it replaces the usual address (e.g. Ollama
    # on another machine: http://192.168.1.20:11434/v1).
    llm_base_url: str | None = None
    max_tokens: int = 1024
    max_tool_iterations: int = 8
    # A reply read in a chat (display "chat") may be longer and take more rounds of tools
    # than a spoken one: the user can wait for it.
    max_chat_tokens: int = 4096
    max_chat_tool_iterations: int = 30

    # --- Claude on a Claude subscription instead of the API (docs/subscription.md) -----
    # Who answers. "api": the provider's API with the key above. "subscription", only
    # with LLM_PROVIDER=anthropic: Claude Code through the Agent SDK (the "subscription"
    # extra), signed in with CLAUDE_CODE_OAUTH_TOKEN, on the user's Claude plan. The API
    # takes what the subscription can't (a file to read, a usage limit, an error) when
    # LLM_API_KEY is set; without a key those turns get a reply that says so.
    engine: Literal["api", "subscription"] = "api"
    # The token `claude setup-token` prints (valid a year).
    claude_code_oauth_token: SecretStr | None = None
    # Empty: LLM_MODEL.
    subscription_model: str = ""
    # Where the subscription answers, among voice, notification, chat: it starts a Claude
    # Code process per turn, a few seconds a chat can wait for and a voice hardly can. The
    # API answers the others; without LLM_API_KEY the subscription answers everywhere.
    subscription_displays: CsvList = ["chat"]

    # --- Home Assistant (optional: without a token, the home tools are off) ------------
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: SecretStr | None = None
    ha_timeout_seconds: float = 10.0
    # HTTP proxy to reach Home Assistant, e.g. a VPN sidecar's outbound proxy.
    ha_proxy_url: str | None = None
    # Only entities in these domains are visible / controllable by the agent.
    ha_exposed_domains: CsvList = [
        "light",
        "switch",
        "cover",
        "climate",
        "fan",
        "media_player",
        "scene",
        "script",
        "vacuum",
        "sensor",
        "binary_sensor",
        "weather",
        "lock",
        "alarm_control_panel",
    ]
    # Actions on these domains need an explicit spoken confirmation in a *later* turn.
    ha_sensitive_domains: CsvList = ["lock", "alarm_control_panel", "cover"]
    # Entities the agent must never see or touch, whatever their domain.
    ha_entity_denylist: CsvList = []

    # --- Home devices driven directly, without Home Assistant (docs/home-devices.md) ---
    # HTTP proxy to reach the home network from elsewhere; HA_PROXY_URL when empty.
    home_proxy_url: str | None = None
    home_timeout_seconds: float = 10.0
    # Philips Hue bridge: its address on the home network and an app key.
    hue_bridge_url: str | None = None
    hue_app_key: SecretStr | None = None
    # Devialet Phantom speaker: its address; no login. Volumes are capped.
    devialet_url: str | None = None
    devialet_max_volume: int = Field(70, ge=0, le=100)
    # A Google TV / Android TV device (a TV, a projector, a box): its address, and the
    # client certificate scripts/android_tv_pair.py had it accept. Needs the android-tv
    # extra. ANDROID_TV_NAME is what the user calls it ("TV", "projector").
    android_tv_host: str | None = None
    android_tv_client_cert: SecretStr | None = None
    android_tv_client_key: SecretStr | None = None
    android_tv_max_volume: int = Field(70, ge=0, le=100)
    android_tv_name: str = "TV"
    # Apps besides the built-in ones (netflix, youtube, prime video, disney+), as JSON:
    # {"name": {"package": "...", "link": "deep link, or null"}}; "name": null drops one.
    android_tv_apps: dict[str, Any] = {}
    # The live TV app (one of the apps) that takes a channel number typed on the remote,
    # and names for those numbers, as JSON: {"news one": 1}. Empty: no live channels.
    android_tv_channel_app: str | None = None
    android_tv_channels: dict[str, Any] = {}
    # Apple TV and HomePods through pyatv (the apple extra): the profile JSON that
    # `scripts/apple_devices.py profile` prints on the home network, and the Apple TV's
    # pairing from `scripts/apple_devices.py pair`. Empty profile: no tool.
    apple_devices: str = ""
    apple_tv_companion_credentials: SecretStr | None = None
    apple_tv_airplay_credentials: SecretStr | None = None
    # Nuki smart lock through the Nuki Web API (token from web.nuki.io > API).
    nuki_api_token: SecretStr | None = None
    # Proof only the user has, asked before a door opens (scripts/unlock_codes_setup.py):
    # an authenticator app's secret and/or a secret phrase. Neither: opening always waits
    # for the user's go in a later message.
    unlock_totp_secret: SecretStr | None = None
    unlock_phrase: SecretStr | None = None
    # Netatmo cameras, weather station, thermostat (scripts/netatmo_login.py).
    netatmo_client_id: str | None = None
    netatmo_client_secret: SecretStr | None = None
    netatmo_refresh_token: SecretStr | None = None
    # PetKit feeder and litter box (cloud, unofficial API: the petkit extra), through a
    # second PetKit account the devices are shared with. PETKIT_REGION: a country code
    # PetKit routes to its regional server; HOME_COUNTRY when empty.
    petkit_username: str | None = None
    petkit_password: SecretStr | None = None
    petkit_region: str | None = None
    # With NTFY_URL: a push when the litter or the food gets under these levels (%), and
    # when the waste bin is full, again every PETKIT_BIN_REMIND_MINUTES; nothing between
    # PETKIT_QUIET_START and PETKIT_QUIET_END (hours of the day).
    petkit_alerts: bool = True
    petkit_litter_alert_percent: int = Field(50, ge=0, le=100)
    petkit_food_alert_percent: int = Field(45, ge=0, le=100)
    petkit_bin_remind_minutes: int = Field(120, ge=1)
    petkit_check_minutes: int = Field(180, ge=1)
    petkit_quiet_start: int = Field(22, ge=0, le=23)
    petkit_quiet_end: int = Field(8, ge=0, le=23)
    # Leaving home (POST /v1/departure, departure.py): what is still unlocked, on or
    # playing among the devices set up, each fix waiting for the user's go.
    departure_check: bool = True

    # --- Assistant persona -------------------------------------------------------------
    assistant_name: str = "Assistant"
    # What the assistant calls the user; empty: no name.
    user_name: str | None = None
    language: str = "en"
    timezone: str = "UTC"
    conversation_ttl_seconds: int = 300
    # A chat (display "chat") is answered hours later: its conversation, with the files
    # sent in it and what waits for the user's go, is kept longer.
    chat_conversation_ttl_seconds: int = 12 * 3600
    max_history_messages: int = 40

    # --- Home location (weather, travel time, web search relevance) --------------------
    home_latitude: float | None = None
    home_longitude: float | None = None
    home_city: str | None = None
    home_country: str | None = None

    # --- Briefing and recap ------------------------------------------------------------
    brief_sections: CsvList = list(BRIEF_SECTIONS)
    weather_detail: Literal["off", "short", "detailed"] = "short"

    # --- Outdoor activities and forecasts (Open-Meteo, no key needed) ------------------
    # Names or aliases from activities.CATALOG, e.g. "surf,wingfoil,cycling".
    outdoor_activities: CsvList = []
    open_meteo_timeout_seconds: float = 10.0
    # Open-Meteo model for wind forecasts; best_match picks one, regional models can be
    # sharper (e.g. meteofrance_seamless, icon_seamless, gfs_seamless).
    wind_model: str = "best_match"
    wind_spots: list[WindSpot] = []
    surf_spots: list[SurfSpot] = []

    # --- Web search (a separate model request that only sees the query) ----------------
    web_search_enabled: bool = True
    # native: the provider's own search tool (anthropic). duckduckgo (no key), searxng
    # (WEB_SEARCH_URL), brave or tavily (WEB_SEARCH_API_KEY): results fetched by the
    # agent, answer written by the model. auto: native where it exists, else duckduckgo.
    web_search_backend: Literal["auto", "native", "duckduckgo", "searxng", "brave", "tavily"] = (
        "auto"
    )
    web_search_url: str | None = None
    web_search_api_key: SecretStr | None = None
    # Empty: a small Claude model with anthropic, LLM_MODEL with the other providers.
    web_search_model: str = ""
    web_search_max_uses: int = 3
    web_search_results: int = 5

    # --- Web pages and investigations (docs/web.md) -------------------------------------
    # Opening public pages and downloading public documents (no logins). Private and
    # internal addresses are always refused.
    web_pages_enabled: bool = True
    # After a request read emails, files or other private content, the only sites it can
    # still open (a URL can carry data out) are the public services of HOME_COUNTRY
    # (tools/web_pages.py, PUBLIC_SERVICES) plus these domains, subdomains included.
    web_trusted_domains: CsvList = []
    # Investigating a photo, a document, a link or a site from public registries and
    # archives (RDAP, the Wayback Machine, crt.sh, OpenStreetMap, DNS): no key needed.
    investigations_enabled: bool = False
    # The agent's own public address (https://agent.example.com), when it has one: lets
    # it put a photo the user sent online for a few minutes, for a reverse image search.
    public_url: str | None = None

    # --- Calendar (CalDAV) -------------------------------------------------------------
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: SecretStr | None = None
    # Only read these calendars (by name); empty means all of them.
    caldav_calendars: CsvList = []
    # Let the agent add events (docs/mail-calendar.md). Off: the calendar is only read.
    caldav_write: bool = False
    # The calendar new events go to, by name; empty: the one the server gives as default.
    caldav_default_calendar: str | None = None

    # --- Contacts' birthdays (CardDAV, read-only) --------------------------------------
    # Shown as all-day events in the calendar. Username and password default to CalDAV's.
    carddav_url: str | None = None
    carddav_username: str | None = None
    carddav_password: SecretStr | None = None

    # --- Apple Reminders, Notes and Find My (iCloud's web services, docs/apple.md) -----
    # The Apple ID logged in by hand with `python -m intelligent_home_agent.icloud_web`;
    # only the session that login leaves is kept, in DATA_DIR/icloud-web. Empty: none
    # of these tools, and reminders are made by the client app on the phone.
    icloud_apple_id: str | None = None
    # Only these Reminders lists (by name); empty means all of them.
    icloud_reminder_lists: CsvList = []
    # Where a new reminder goes without a list; empty: the list with the most open ones.
    icloud_reminders_default_list: str | None = None
    icloud_notes: bool = True
    icloud_find_my: bool = True

    # --- Email (IMAP; each account is read-only unless it says "write": true) ----------
    mail_accounts: list[MailAccount] = []

    # --- Travel time from home (OpenStreetMap: Nominatim + OSRM, no key) ---------------
    travel_enabled: bool = True
    travel_margin_minutes: int = 10
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    osrm_url: str = "https://router.project-osrm.org"

    # --- GitHub (token that can read the user's repositories) --------------------------
    github_token: SecretStr | None = None
    # Let the agent list the user's repositories, read and search their files, and check
    # their dependencies for known flaws (OSV.dev). Read-only.
    github_repositories: bool = False

    # --- Notes repository (docs/tasks.md): reminders and handoff notes for coding sessions
    # A repository of notes the token can read, "owner/repo": reminders left with a coding
    # assistant, one Markdown file each. Empty: no assistant reminders.
    # ASSISTANT_REMINDERS_REPO is its former name.
    notes_repo: str | None = Field(
        None, validation_alias=AliasChoices("notes_repo", "assistant_reminders_repo")
    )
    assistant_reminders_path: str = "reminders"
    handoffs_path: str = "handoffs"
    # Let the agent leave reminders and handoff notes there and close reminders (the token
    # must be allowed to write the repository's contents). Off: the repository is only read.
    notes_write: bool = False
    # Optional, for a repository that encrypts folders with git-crypt: the key it exported
    # (git-crypt export-key), as one base64 line. Empty: MEMORY_CRYPT_KEY when the memory
    # is in the same repository.
    notes_crypt_key: SecretStr | None = None

    # --- Tasks the agent runs by itself (docs/tasks.md) ---------------------------------
    # They need a way to reach the user (NTFY_URL); without one, there are none.
    # local: Markdown files in DATA_DIR/tasks. github: a folder of NOTES_REPO (the token
    # must be allowed to write its contents). off: none.
    tasks_store: Literal["local", "github", "off"] = "local"
    # Where the folder is. Empty: tasks, in DATA_DIR (local) or in a folder named after
    # the assistant (github: "Nova" keeps nova/tasks).
    tasks_path: str = ""
    tasks_poll_seconds: float = Field(60, gt=0)
    # The briefing and the recap, sent every day at that local time ("07:30"). Empty: only
    # when asked.
    morning_briefing_at: str | None = None
    evening_recap_at: str | None = None

    # --- Health: Withings scale (weigh-ins, read-only; docs/health.md) -----------------
    withings_client_id: str | None = None
    withings_client_secret: SecretStr | None = None
    withings_refresh_token: SecretStr | None = None

    # --- Parcels the agent notes when it reads a shipping email (needs MAIL_ACCOUNTS) ---
    deliveries_enabled: bool = True

    # --- Connectors: remote MCP servers (a bank, a hosting provider...), JSON list ------
    mcp_servers: list[McpServer] = []

    # --- Documents, PDFs and pictures (docs/documents.md) ------------------------------
    # The files of the conversation listed and opened, PDFs written, forms filled, pages
    # copied, drawings rendered. Nothing leaves the machine. What needs the "documents"
    # extra is simply absent without it.
    documents_enabled: bool = True
    # auto: US Letter where it is the usual paper (from HOME_COUNTRY), else A4.
    paper_size: Literal["auto", "a4", "letter"] = "auto"
    # A folder with the fonts to write PDFs and drawings in (a Regular and a Bold .ttf or
    # .otf of one family). Empty: DejaVu Sans, which the Docker image installs.
    pdf_font_dir: str | None = None
    # Photos of gym and home exercises from a public-domain bank on GitHub.
    exercise_photos_enabled: bool = False
    # Calculations and charts run as Python in the model provider's sandbox, billed there.
    # Anthropic only (it needs a code execution tool and a Files API).
    code_execution_enabled: bool = False
    # Empty: LLM_MODEL.
    code_execution_model: str = ""

    # --- Pictures and videos made by an AI model, billed by its provider ---------------
    # Google (AI Studio key): Gemini's image model and Veo.
    generation_gemini_api_key: SecretStr | None = None
    generation_gemini_image_model: str = "gemini-2.5-flash-image"
    generation_gemini_video_model: str = "veo-3.1-lite-generate-001"
    # OpenAI (gpt-image, Sora), or with GENERATION_OPENAI_BASE_URL any server that speaks
    # OpenAI's images API (a local one needs no key). A model set to "off": none of that
    # kind, e.g. GENERATION_OPENAI_VIDEO_MODEL=off for a server that only makes pictures.
    generation_openai_api_key: SecretStr | None = None
    generation_openai_base_url: str | None = None
    generation_openai_image_model: str = "gpt-image-2"
    generation_openai_video_model: str = "sora-2"
    # The provider used when the model names none; the first one configured when empty.
    generation_default_provider: Literal["", "gemini", "openai"] = ""
    # Most of each a single request may make.
    generation_max_images: int = Field(6, ge=0)
    generation_max_videos: int = Field(2, ge=0)

    # --- What the agent keeps on disk --------------------------------------------------
    # Where the agent keeps what must survive a restart (the journal, a connector's rotated
    # login); a volume in Docker.
    data_dir: str = "data"
    # The journal: one line per request (what was asked, the tools called, the reply) in
    # DATA_DIR/journal, one file per day, removed after this many days. 0: no journal.
    journal_keep_days: int = Field(365, ge=0)
    # Files sent in a chat or made by the agent, kept in DATA_DIR/files for this many
    # days, so they survive a restart and can be used again later. 0: memory only.
    files_keep_days: int = Field(30, ge=0)

    # --- Memory: facts about the user and how-tos that worked (docs/memory.md) ---------
    # local: Markdown files in DATA_DIR. github: paths of a repository (MEMORY_REPO, with
    # GITHUB_TOKEN allowed to write its contents), editable from any computer. off: none.
    memory_store: Literal["local", "github", "off"] = "local"
    # "owner/repo", for MEMORY_STORE=github.
    memory_repo: str | None = None
    # Where the two files are. Empty: memory.md and procedures.md, in DATA_DIR (local) or
    # in a folder named after the assistant (github: "Nova" keeps nova/memory.md).
    memory_path: str = ""
    procedures_path: str = ""
    # Optional, for a repository that encrypts that folder with git-crypt: the key it
    # exported (git-crypt export-key), as one base64 line.
    memory_crypt_key: SecretStr | None = None

    # --- Knowledge base (the kb container of compose/kb.yml, docs/knowledge.md) --------
    kb_url: str | None = None
    kb_token: SecretStr | None = None

    # --- File drives through an rclone sidecar (compose/drive.yml, docs/drives.md) -----
    # Where the sidecar's API is (set by compose/drive.yml) and the password both share.
    # Tools exist once the URL, the password and at least one drive are set.
    drive_url: str | None = None
    drive_user: str = "agent"
    drive_password: SecretStr | None = None
    # The drives, a JSON list: name, remote, and optionally label, backend, read_only,
    # content.
    drives: list[Drive] = []
    # How old the index of file names may get before it is rebuilt.
    drive_index_hours: float = Field(24, gt=0)
    # Copies of what the agent deletes, in DATA_DIR/drive-trash: how long they are kept,
    # the biggest item copied and the most the trash may hold. Beyond that, a deletion
    # waits for the user to accept it without a copy.
    drive_trash_days: float = Field(30, gt=0)
    drive_trash_max_item_mb: int = Field(1024, ge=0)
    drive_trash_max_total_mb: int = Field(5120, ge=0)
    # Index the text inside the files (PDF, office, text) for search by content.
    drive_content: bool = True
    # Scanned PDFs: "model" has the model read them (a few per hour, each a paid request;
    # needs a provider that takes PDFs, anthropic today), "off" lists them by name only.
    drive_ocr: Literal["off", "model"] = "off"
    # Empty: a small Claude model with anthropic.
    drive_ocr_model: str = ""

    # --- Push notifications (ntfy, optional) -------------------------------------------
    # Full topic URL, e.g. https://ntfy.sh/some-long-random-topic or a self-hosted server.
    ntfy_url: str | None = None
    ntfy_token: SecretStr | None = None

    @field_validator(
        "ha_exposed_domains",
        "ha_sensitive_domains",
        "ha_entity_denylist",
        "caldav_calendars",
        "icloud_reminder_lists",
        "brief_sections",
        "outdoor_activities",
        "web_trusted_domains",
        "subscription_displays",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("brief_sections")
    @classmethod
    def _known_sections(cls, value: list[str]) -> list[str]:
        unknown = [s for s in value if s not in BRIEF_SECTIONS]
        if unknown:
            raise ValueError(f"unknown sections {unknown}; known: {', '.join(BRIEF_SECTIONS)}")
        return value

    @field_validator("outdoor_activities")
    @classmethod
    def _known_activities(cls, value: list[str]) -> list[str]:
        return [activity.key for activity in resolve(value)]

    @field_validator("agent_api_token")
    @classmethod
    def _not_the_placeholder(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() in {"", "change-me"}:
            raise ValueError("set AGENT_API_TOKEN to a random value (see .env.example)")
        return value

    @field_validator("ha_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("mcp_servers")
    @classmethod
    def _distinct_connectors(cls, value: list[McpServer]) -> list[McpServer]:
        names = [server.name for server in value]
        if len(set(names)) != len(names):
            raise ValueError("two MCP_SERVERS entries share a name")
        return value

    @field_validator("drives")
    @classmethod
    def _distinct_drives(cls, value: list[Drive]) -> list[Drive]:
        names = [drive.name for drive in value]
        if len(set(names)) != len(names):
            raise ValueError("two DRIVES entries share a name")
        return value

    @property
    def drives_enabled(self) -> bool:
        return bool(self.drives and self.drive_url and self.drive_password)

    @property
    def subscription_enabled(self) -> bool:
        return self.engine == "subscription" and self.claude_code_oauth_token is not None

    @property
    def drive_ocr_enabled(self) -> bool:
        """Scans are only read where the provider takes a PDF as a document; elsewhere
        DRIVE_OCR=model falls back to the text layers alone (main logs it)."""
        return self.drive_ocr == "model" and self.llm_provider in DOCUMENT_PROVIDERS

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        value = value.strip().casefold()
        if value not in PROVIDERS:
            raise ValueError(f"unknown provider; available: {', '.join(PROVIDERS)}")
        return value

    @model_validator(mode="after")
    def _model_is_usable(self) -> "Settings":
        if self.engine == "subscription":
            # Checked first: with it, Claude needs no API key.
            if self.llm_provider != ANTHROPIC:
                raise ValueError(
                    "ENGINE=subscription is Claude on a Claude plan: it needs "
                    f"LLM_PROVIDER=anthropic, not {self.llm_provider}"
                )
            if self.claude_code_oauth_token is None:
                raise ValueError(
                    "ENGINE=subscription needs CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token`)"
                )
            unknown = set(self.subscription_displays) - {"voice", "notification", "chat"}
            if unknown:
                raise ValueError(
                    f"SUBSCRIPTION_DISPLAYS: unknown {sorted(unknown)}; known: voice, "
                    "notification, chat"
                )
        elif needs_key(self.llm_provider) and self.llm_api_key is None:
            raise ValueError(f"LLM_PROVIDER={self.llm_provider} needs LLM_API_KEY")
        if self.llm_provider == CUSTOM and not self.llm_base_url:
            raise ValueError("LLM_PROVIDER=openai-compatible needs LLM_BASE_URL")
        if not self.llm_model:
            if self.llm_provider != ANTHROPIC:
                raise ValueError(f"set LLM_MODEL to a model of {self.llm_provider}")
            self.llm_model = DEFAULT_ANTHROPIC_MODEL
        if not self.drive_ocr_model:
            self.drive_ocr_model = (
                DEFAULT_ANTHROPIC_SEARCH_MODEL if self.llm_provider == ANTHROPIC else self.llm_model
            )
        return self

    @field_validator("morning_briefing_at", "evening_recap_at")
    @classmethod
    def _a_time_of_day(cls, value: str | None) -> str | None:
        if value is None:
            return None
        hours, sep, minutes = value.strip().partition(":")
        if not (sep and hours.isdigit() and minutes.isdigit() and len(minutes) == 2):
            raise ValueError('a time of day is "HH:MM", e.g. 07:30')
        if int(hours) > 23 or int(minutes) > 59:
            raise ValueError('a time of day is "HH:MM", e.g. 07:30')
        return f"{int(hours):02d}:{minutes}"

    @model_validator(mode="after")
    def _tasks_are_usable(self) -> "Settings":
        if self.tasks_store == "github" and not (self.github_token and self.notes_repo):
            raise ValueError("TASKS_STORE=github needs GITHUB_TOKEN and NOTES_REPO")
        return self

    @model_validator(mode="after")
    def _memory_is_usable(self) -> "Settings":
        if self.memory_store == "github" and not (self.github_token and self.memory_repo):
            raise ValueError("MEMORY_STORE=github needs GITHUB_TOKEN and MEMORY_REPO")
        return self

    @model_validator(mode="after")
    def _search_is_usable(self) -> "Settings":
        native = self.llm_provider in WEB_SEARCH_PROVIDERS
        if self.web_search_backend == "auto":
            self.web_search_backend = "native" if native else "duckduckgo"
        if not self.web_search_enabled:
            return self
        if self.web_search_backend == "native" and not native:
            raise ValueError(
                f"WEB_SEARCH_BACKEND=native needs a provider with its own search tool "
                f"({', '.join(sorted(WEB_SEARCH_PROVIDERS))}), not {self.llm_provider}"
            )
        if self.web_search_backend == "searxng" and not self.web_search_url:
            raise ValueError("WEB_SEARCH_BACKEND=searxng needs WEB_SEARCH_URL")
        if self.web_search_backend in {"brave", "tavily"} and self.web_search_api_key is None:
            raise ValueError(
                f"WEB_SEARCH_BACKEND={self.web_search_backend} needs WEB_SEARCH_API_KEY"
            )
        if not self.web_search_model:
            self.web_search_model = (
                DEFAULT_ANTHROPIC_SEARCH_MODEL
                if self.web_search_backend == "native"
                else self.llm_model
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
