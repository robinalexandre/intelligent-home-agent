"""Runtime configuration, loaded from environment variables (or a .env file).

Run ``python scripts/setup.py`` to write a .env interactively; .env.example lists every
setting with a comment.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from intelligent_home_agent.activities import resolve
from intelligent_home_agent.llm import (
    ANTHROPIC,
    CUSTOM,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_ANTHROPIC_SEARCH_MODEL,
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

    # --- Assistant persona -------------------------------------------------------------
    assistant_name: str = "Assistant"
    language: str = "en"
    timezone: str = "UTC"
    conversation_ttl_seconds: int = 300
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

    # --- Calendar (CalDAV, read-only) --------------------------------------------------
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: SecretStr | None = None
    # Only read these calendars (by name); empty means all of them.
    caldav_calendars: CsvList = []

    # --- Email (IMAP, read-only) -------------------------------------------------------
    mail_accounts: list[MailAccount] = []

    # --- Travel time from home (OpenStreetMap: Nominatim + OSRM, no key) ---------------
    travel_enabled: bool = True
    travel_margin_minutes: int = 10
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    osrm_url: str = "https://router.project-osrm.org"

    # --- GitHub (token that can read the user's repositories) --------------------------
    github_token: SecretStr | None = None
    # Reminders left with a coding assistant, one Markdown file each in a repo the token
    # can read: "owner/repo". Empty: no assistant reminders.
    assistant_reminders_repo: str | None = None
    assistant_reminders_path: str = "reminders"

    # --- Knowledge base (the kb container of compose/kb.yml, docs/knowledge.md) --------
    kb_url: str | None = None
    kb_token: SecretStr | None = None

    # --- Push notifications (ntfy, optional) -------------------------------------------
    # Full topic URL, e.g. https://ntfy.sh/some-long-random-topic or a self-hosted server.
    ntfy_url: str | None = None
    ntfy_token: SecretStr | None = None

    @field_validator(
        "ha_exposed_domains",
        "ha_sensitive_domains",
        "ha_entity_denylist",
        "caldav_calendars",
        "brief_sections",
        "outdoor_activities",
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

    @field_validator("llm_provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        value = value.strip().casefold()
        if value not in PROVIDERS:
            raise ValueError(f"unknown provider; available: {', '.join(PROVIDERS)}")
        return value

    @model_validator(mode="after")
    def _model_is_usable(self) -> "Settings":
        if needs_key(self.llm_provider) and self.llm_api_key is None:
            raise ValueError(f"LLM_PROVIDER={self.llm_provider} needs LLM_API_KEY")
        if self.llm_provider == CUSTOM and not self.llm_base_url:
            raise ValueError("LLM_PROVIDER=openai-compatible needs LLM_BASE_URL")
        if not self.llm_model:
            if self.llm_provider != ANTHROPIC:
                raise ValueError(f"set LLM_MODEL to a model of {self.llm_provider}")
            self.llm_model = DEFAULT_ANTHROPIC_MODEL
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
