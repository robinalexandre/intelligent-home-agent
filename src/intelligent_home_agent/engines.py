"""The subscription engines of the server, one per model (ENGINE=subscription).

The chat's turns run on one model, the server's own model calls (a web search, a scan to
read) on smaller ones: each gets the user's Claude plan first and the API only when the
plan can't take it (engine.subscription_first). None when the subscription is off, or
when the Agent SDK isn't installed (the "subscription" extra): the API takes everything.
"""

from __future__ import annotations

import logging
from pathlib import Path

from intelligent_home_agent.config import Settings
from intelligent_home_agent.engine import ClaudeCodeEngine, sdk_installed

log = logging.getLogger(__name__)

MISSING_SDK = (
    "ENGINE=subscription needs the Claude Agent SDK, which isn't installed: "
    'pip install ".[subscription]", or with Docker EXTRAS=documents,subscription in .env '
    "and docker compose up -d --build (docs/subscription.md)."
)

_ENGINES: dict[tuple[str, str], ClaudeCodeEngine] = {}


def config_dir(settings: Settings) -> Path:
    """Claude Code's own files, with what the agent keeps."""
    if settings.claude_config_dir:
        return Path(settings.claude_config_dir)
    return Path(settings.data_dir) / "claude"


def check_subscription(settings: Settings) -> None:
    """Said once at startup. With an API key the agent still works without the SDK, on
    the API alone; without a key nothing could answer, so it doesn't start."""
    if not settings.subscription_enabled or sdk_installed():
        return
    if settings.llm_api_key is None:
        raise RuntimeError(MISSING_SDK + " Or set LLM_API_KEY and ENGINE=api.")
    log.warning("%s Until then the API answers everything.", MISSING_SDK)


def subscription_engine(settings: Settings, model: str) -> ClaudeCodeEngine | None:
    token = settings.claude_code_oauth_token
    if not settings.subscription_enabled or token is None or not sdk_installed():
        return None
    folder = config_dir(settings)
    key = (model, str(folder))
    if key not in _ENGINES:
        _ENGINES[key] = ClaudeCodeEngine(
            model=model, token=token.get_secret_value(), config_dir=folder
        )
    return _ENGINES[key]
