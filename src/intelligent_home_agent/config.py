"""Runtime configuration, loaded from environment variables (or a .env file).

Run ``python scripts/setup.py`` to write a .env interactively; .env.example lists every
setting with a comment.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from intelligent_home_agent.activities import resolve

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
    llm_provider: str = "anthropic"
    llm_api_key: SecretStr = Field(
        validation_alias=AliasChoices("llm_api_key", "anthropic_api_key")
    )
    llm_model: str = Field(
        "claude-sonnet-5-5", validation_alias=AliasChoices("llm_model", "anthropic_model")
    )
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

    # --- Web search (a separate model request with the provider's search tool) ---------
    web_search_enabled: bool = True
    web_search_model: str = "claude-haiku-5-5"
    web_search_max_uses: int = 3

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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
