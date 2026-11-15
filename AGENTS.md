# AGENTS.md

Notes for any coding agent (and person) working in this repository.

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate   # or: uv venv && uv pip install ...
pip install -e ".[dev]"
```

## Checks (all three must pass, CI runs them)

```bash
ruff check .
ruff format --check .        # `ruff format .` to fix
pytest -q
```

Tests never touch the network: HTTP goes through `respx`, the model through `FakeLLM` in
`tests/conftest.py`. If your shell exports `GITHUB_TOKEN`, `ANTHROPIC_API_KEY` or other
settings, tests that build `Settings` clear the ones they care about; when in doubt run
`env -u GITHUB_TOKEN pytest -q`.

## Layout

```
src/intelligent_home_agent/
  main.py           FastAPI app: /v1/ask, /v1/briefing, auth, tool registry wiring
  agent.py          tool-use loop, taint tracking, memory scrubbing
  llm.py            provider interface (LLMClient, LLMResponse), the Anthropic client and
                    the OpenAI-compatible one (OpenAI, Mistral, Ollama...)
  prompts.py        system prompt, built from what is configured
  i18n.py           strings per language (fallback reply, closing words, examples)
  activities.py     outdoor activity catalog (stdlib only, used by scripts/setup.py)
  config.py         settings (env / .env)
  conversations.py  short-lived memory and pending confirmations
  notify.py         ntfy push
  home_devices.py   devices driven without Home Assistant (Hue, Devialet)
  tools/            one module per capability, each with a build_*_tools() factory
scripts/setup.py         interactive .env writer (stdlib only)
compose/                 one Compose file per way to reach the agent, picked by COMPOSE_FILE
scripts/build_shortcuts.py  Siri Shortcuts builder (macOS)
```

## Conventions

- Python 3.12+, type hints, `from __future__ import annotations`, line length 100.
- A tool is a `Tool(name, description, input_schema, handler)`; its factory returns `[]`
  when it isn't configured, so the registry and the prompt only show what works.
- Errors meant for the model are `ToolError`; anything else is logged as a crash.
- Security flags on tools, enforced in `ToolRegistry.run` and `Agent`:
  - `taints=True` for output written by other people (mail, calendar, GitHub, reminders).
  - `needs_clean_turn=True` for anything that acts on the home or sends data out.
  - Never weaken these, the entity policy in `tools/home_assistant.py`, or the
    later-turn confirmation for sensitive domains, without a test that shows why.
- New settings go in `config.py`, `.env.example` (with a comment), and the configuration
  table in README.md; ask for them in `scripts/setup.py` if a new user needs them.
- A new way to deploy is a file in `compose/` that adds to `docker-compose.yml`, a choice
  in `ACCESS` in `scripts/setup.py`, and a row in docs/setup.md.
- New user-facing words go through `i18n.py` (English first, French kept up to date).
- Test data stays fictional: `0.0, 0.0` or famous public places, names like "Alex",
  `example.com` addresses.
- Docs: plain and short; say what works and what doesn't.

## Adding an LLM provider

A server that speaks the OpenAI Chat Completions API needs no code: add it to
`OPENAI_COMPATIBLE` in `llm.py` and to `MODELS` in `scripts/setup.py` (a test keeps the two
in step). For another API:

1. In `llm.py`, write a class with `async def create(self, **kwargs) -> LLMResponse`.
   It receives `model`, `max_tokens`, `system`, `tools` (`name`, `description`,
   `input_schema`) and `messages` in the Messages format used throughout the code
   (`text`, `tool_use`, `tool_result` blocks). Convert to the vendor's format and back;
   map the stop reason to `end_turn`, `tool_use` or `max_tokens`.
2. Make `build_llm_client` return it, and add its name to `PROVIDERS`. Add it to
   `WEB_SEARCH_PROVIDERS` only if it can run the
   server-side search tool used by `tools/web.py`; otherwise web search stays off.
3. Add a test with a fake vendor client covering a tool call round trip.

## Adding an outdoor activity

Add an `Activity` to `CATALOG` in `activities.py`: kind `weather` with limits (rain,
wind, temperature, visibility) is enough for `get_outdoor_forecast` to handle it. Kinds
`wind` and `surf` reuse the per-spot tools. Add aliases in the languages you know.
