# intelligent-home-agent

A self-hosted assistant for the home: a language model as the brain, Home Assistant as
the hands, and whatever voice or app you already use as the way in (Siri, Home Assistant
Assist, Google Home or Alexa routines, push notifications, a scheduled task).

```
Siri / HA Assist / routine / scheduler ──HTTPS──► intelligent-home-agent ──► LLM provider
                                                        │
                                                        ├─► Home Assistant (private network)
                                                        └─► Open-Meteo, CalDAV, IMAP, OSM, GitHub
```

Plain commands ("turn off the living room") are best left to your voice assistant's own
Home Assistant integration: instant and local. This agent handles what needs reasoning:
multi-step requests, questions about the house, the weather for what you plan to do, and a
spoken morning briefing and evening recap.

## What it does

- `POST /v1/ask`: one request in, one short reply out, with a few minutes of memory per
  conversation so follow-ups work ("and the kitchen too"). The reply is written to be
  spoken, shown as a notification or read in a chat (`display`), and the request can carry
  files (a PDF, a photo, a text or office document) for the agent to read.
- `POST /v1/briefing`: the morning briefing or evening recap, written to be read, optionally
  pushed to your phone and desktop through ntfy. Made for schedulers.
- Tools, each one on only when configured:
  - Home Assistant: find devices, read states, call services, HA weather.
  - Some devices directly, without Home Assistant: Philips Hue lights, a Devialet speaker
    ([docs/home-devices.md](docs/home-devices.md)).
  - Weather at home or anywhere (Open-Meteo, no key).
  - Outdoor activities: wind sports per spot (strength, gusts, direction), surf per beach
    (waves, period, wind effect, tides), and weather windows for everything else
    (cycling, running, hiking, skiing, climbing, golf, or just "I go out a lot").
  - Calendar (CalDAV) and mailboxes (IMAP), both read-only.
  - When to leave for an appointment (driving time from home, OpenStreetMap).
  - GitHub pull requests waiting for you, and reminders left with a coding assistant.
  - Connectors: any service that has an MCP server, such as your bank or your hosting
    provider, read freely and changed only on your yes
    ([docs/connectors.md](docs/connectors.md)).
  - Your own notes (a knowledge base in a GitHub repository), searched before it says it
    doesn't know: see [docs/knowledge.md](docs/knowledge.md).
  - Web search with any model, in a separate model request that only sees the query.
  - Reminders created on the phone by the Siri Shortcut from the agent's `actions`.
- The briefing and recap follow your settings: which sections, how detailed the weather,
  which activities. The language of the replies and of the few fixed strings is a setting
  (English by default, French supported).

## Quick start

```bash
python3 scripts/setup.py              # asks a few questions, writes .env
docker compose up -d --build
curl -s localhost:8000/health
```

**[Setup, step by step](docs/setup.md)** walks through every choice: where it runs (a
home server, a Raspberry Pi, a VPS, with or without Docker), which model (Claude, OpenAI,
Mistral, Gemini, a local model with Ollama...), how it reaches Home Assistant, how you
reach it (home network, Tailscale, Cloudflare Tunnel, your own domain) and how you talk
to it.

### For development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python scripts/setup.py               # or: cp .env.example .env and edit it
uvicorn intelligent_home_agent.main:app --reload

curl -s localhost:8000/v1/ask \
  -H "Authorization: Bearer $AGENT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Which lights are on?"}'
```

Tests and lint: `pytest -q && ruff check . && ruff format --check .` (see
[AGENTS.md](AGENTS.md)). Before exposing the agent, read [deployment](docs/deployment.md).

## Configuration

Environment variables or `.env`. `scripts/setup.py` writes the common ones;
[.env.example](.env.example) lists all of them with comments.

| Setting | Default | What it does |
|---|---|---|
| `AGENT_API_TOKEN` | required | Bearer token every client sends. |
| `LLM_PROVIDER` | `anthropic` | Model provider. See "Model providers" below. |
| `LLM_API_KEY` | required, except local servers | Provider API key (`ANTHROPIC_API_KEY` also accepted). |
| `LLM_MODEL` | `claude-sonnet-5-5` for Anthropic, else required | Model for the conversation; it must support tool calling. |
| `LLM_BASE_URL` | the provider's | Address of an OpenAI-compatible server. |
| `MAX_TOKENS`, `MAX_TOOL_ITERATIONS` | `1024`, `8` | Length of a spoken reply, and how many rounds of tools one request may take. |
| `MAX_CHAT_TOKENS`, `MAX_CHAT_TOOL_ITERATIONS` | `4096`, `30` | The same for a reply read in a chat (`"display": "chat"`). |
| `ASSISTANT_NAME` | `Assistant` | What the assistant calls itself; also closes a conversation ("thanks, Assistant"). |
| `USER_NAME` | unset | What the assistant calls you. |
| `LANGUAGE` | `en` | `en` or `fr`. |
| `TIMEZONE` | `UTC` | IANA time zone. |
| `HOME_LATITUDE`, `HOME_LONGITUDE` | unset | Home, for weather, travel time and outdoor forecasts. |
| `HOME_CITY`, `HOME_COUNTRY` | unset | Local web search results. |
| `BRIEF_SECTIONS` | all | Any of `calendar,travel,mail,pull_requests,reminders,weather,activities`. |
| `WEATHER_DETAIL` | `short` | `off`, `short` (one sentence) or `detailed`. |
| `OUTDOOR_ACTIVITIES` | none | e.g. `surf,wingfoil,cycling`; names or aliases from `activities.py`. |
| `WIND_SPOTS`, `SURF_SPOTS`, `WIND_MODEL` | none, none, `best_match` | Spots for the wind and surf tools (JSON). |
| `HA_URL`, `HA_TOKEN` | local URL, unset | Home Assistant; no token means no home tools. |
| `HA_EXPOSED_DOMAINS` | common domains | Only these entity domains are visible. |
| `HA_SENSITIVE_DOMAINS` | `lock,alarm_control_panel,cover` | Need a spoken yes in a later request. |
| `HA_ENTITY_DENYLIST` | empty | Entities the agent never sees. |
| `HA_PROXY_URL` | unset | HTTP proxy to reach HA (VPN sidecar). |
| `HUE_BRIDGE_URL`, `HUE_APP_KEY` | unset | Philips Hue lights, straight from the bridge. |
| `DEVIALET_URL`, `DEVIALET_MAX_VOLUME` | unset, `70` | A Devialet Phantom speaker. |
| `WEB_SEARCH_ENABLED`, `WEB_SEARCH_BACKEND` | `true`, `auto` | Web search: `native` (Anthropic's own tool), or `duckduckgo`, `searxng`, `brave`, `tavily` with any model. |
| `WEB_SEARCH_URL`, `WEB_SEARCH_API_KEY`, `WEB_SEARCH_MODEL` | unset | SearXNG address; Brave or Tavily key; model that writes the answer. |
| `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`, `CALDAV_CALENDARS` | unset | Calendar, read-only. |
| `MAIL_ACCOUNTS` | `[]` | IMAP accounts (JSON), read-only. |
| `TRAVEL_ENABLED`, `TRAVEL_MARGIN_MINUTES` | `true`, `10` | When to leave (needs home). |
| `GITHUB_TOKEN` | unset | Pull requests waiting for you. |
| `ASSISTANT_REMINDERS_REPO`, `ASSISTANT_REMINDERS_PATH` | unset, `reminders` | Coding-assistant reminders: `owner/repo` and folder. |
| `MCP_SERVERS` | `[]` | Connectors: remote MCP servers (JSON), each with its login and `writes`: `off` or `confirm`. |
| `KB_URL`, `KB_TOKEN` | unset | Knowledge base container (`compose/kb.yml`, with `KB_REPO` and `KB_GITHUB_TOKEN`). |
| `NTFY_URL`, `NTFY_TOKEN` | unset | Push channel for `/v1/briefing`. |
| `CONVERSATION_TTL_SECONDS` | `300` | How long a conversation is remembered, with the files sent in it and what waits for your yes. |
| `DATA_DIR` | `data` | Where the agent keeps what must survive a restart (the `agent_data` volume in Docker). |
| `JOURNAL_KEEP_DAYS` | `365` | Days the journal of requests is kept in `DATA_DIR/journal`; `0` turns it off. |

### Outdoor activities

Each activity maps to the forecast data it needs:

| Activity | Kind | Forecasts | Tool |
|---|---|---|---|
| surf | per beach | waves, period, tides, wind | `get_surf_forecast` (`SURF_SPOTS`) |
| wingfoil, kitesurf, windsurf, sailing | per spot | wind, gusts, direction (tides, waves) | `get_wind_forecast` (`WIND_SPOTS`) |
| cycling, running, hiking, skiing, climbing, golf, outdoors | weather | rain, wind, temperature, UV, snow, visibility (per activity) | `get_outdoor_forecast` |

Free text works in the setup script ("I bike and go out a lot" gives `cycling,outdoors`).
Wind activities without spots fall back to the general weather. To add an activity, see
[AGENTS.md](AGENTS.md).

### Coding-assistant reminders

`get_assistant_reminders` reads one Markdown file per reminder from a GitHub repo, with a
front matter of `title`, `on` (when it starts showing), `until` (last day), and optional
`where` and `machine`. Any coding agent can write these files; the format is in
`tools/assistant_reminders.py`.

### Model providers

The agent loop, memory and tools speak one message format and call the model through a
small interface (`LLMClient` in `llm.py`). Two clients are shipped:

- **Anthropic** (`LLM_PROVIDER=anthropic`), the default;
- **OpenAI-compatible**, for every server that speaks the Chat Completions API: `openai`,
  `mistral`, `gemini`, `groq`, `openrouter`, local models with `ollama` or `lmstudio`, and
  `openai-compatible` with `LLM_BASE_URL` for anything else (llama.cpp, vLLM, a gateway).

[Setup, step 2](docs/setup.md#2-the-model) has the table. A provider with another API is
one class that converts requests and responses (steps in [AGENTS.md](AGENTS.md)).

Web search works with all of them: Anthropic runs its own server-side search tool; with
the others the agent queries a search engine (DuckDuckGo, your SearXNG, Brave or Tavily)
and the model you chose writes the answer from the results, in a request of its own.

## Integrations

Siri Shortcuts, Home Assistant Assist, Google Home and Alexa (through Home Assistant),
ntfy push, and scheduled tasks (cron, Home Assistant, a chat or coding-assistant app):
see [docs/integrations.md](docs/integrations.md) and [docs/siri-shortcut.md](docs/siri-shortcut.md).

## Security model

The guardrails are enforced in code, not only in the prompt.

- **Authentication**: every endpoint except `/health` needs the bearer token, compared in
  constant time.
- **Entity policy**: only entities in `HA_EXPOSED_DOMAINS` are visible, denylisted ones
  never are. A service can only target entities of its own domain (no `light.turn_on` on a
  lock), and targets can't be smuggled in through `data`.
- **Confirmation in a later turn**: Home Assistant actions on sensitive domains are parked
  and only run when the user says yes in a *later* message. The model can't confirm for
  itself within the same request.
- **Taints**: tools whose output is written by other people (emails, calendar events, pull
  requests, assistant reminders, a connected account, a file you send, a web search
  answer) mark the request as tainted. From the next tool batch on, tools flagged
  `needs_clean_turn` (home control, any write) don't run for the rest of that request, so
  injected text can't trigger actions. Travel time, which sends a destination out, then
  only accepts a place read from the calendar in that request.
- **Your go, on exactly what you were shown**: an action held back that way is not lost.
  It is parked with its exact arguments, and the reply ends with a numbered list written
  by the code, not by the model:

  ```
  Waiting for your go ("yes", or "yes except 2"):
  1. Search the web for "bakery opening hours" and carry on
  2. My Bank: create_client {"name": "Acme"}
  ```

  It runs only when your next message agrees, in a request that has read nothing else,
  and never in a scheduled briefing. Nothing read in between can change what it does;
  what was never listed can't run; it is forgotten with the conversation.
- **Web search**: a query is text that leaves the house. After private content was read
  the search waits for your go like a write; in any request, a query that repeats a
  number, an email address, a long token or a run of words from what the request holds
  (earlier replies, tool results) is refused; at most three searches run per request.
- **Lockdown of memory**: after a tainted request, only the question, the reply and the
  names of the tools used stay in conversation memory; the private content is dropped, so
  it can't steer later requests. A request that fails is rolled back entirely. The
  journal keeps neither the reply nor the tool arguments of a tainted request.
- **Connectors**: reading from a connected service taints the request like an email. A
  write never runs when asked, even in a clean request: it waits for your go as above,
  shown with its exact arguments; with `writes: off` the write tools don't exist.
- **Files**: a file you send is read as data written by someone else (it taints the
  request), its name is reduced to a safe character set, and an office file that would
  expand like a zip bomb is not opened.
- **Read-only sources**: IMAP mailboxes are opened read-only and never marked as read;
  CalDAV is only queried.
- **Secrets** stay in `.env` (mode 600 when written by the setup script); the setup script
  never prints them. The Siri Shortcut files hold the token: don't share them.

## Project layout

```
src/intelligent_home_agent/   app, agent loop, prompts, settings, tools/
scripts/                      setup.py (onboarding), build_shortcuts.py (Siri), mcp_login.py
compose/                      one file per way to reach the agent (Caddy, Tailscale...)
docs/                         setup step by step, deployment, integrations, Siri Shortcut
tests/                        pytest, no network
```

## Roadmap

- [x] A second model provider (OpenAI-compatible endpoint, local models)
- [x] Web search with providers other than Anthropic
- [ ] Optional: Home Assistant's MCP server instead of the REST tools
- [ ] Package as a Home Assistant add-on
- [ ] Rate limiting and request logs

## License

MIT, see [LICENSE](LICENSE).
