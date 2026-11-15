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
- A chat with it in WhatsApp (through a WAHA gateway you run) or Telegram (through a
  bot of your own): write, send files and voice messages, approve what it proposes
  with a tap, and get its briefings and the tasks it runs by itself there
  ([docs/chat.md](docs/chat.md)).
- `POST /v1/departure`: called when you leave home (a phone automation), it says what
  is still unlocked, on or playing among the devices set up, and turns it off on your
  yes ([docs/home-devices.md](docs/home-devices.md#leaving-home)).
- Tools, each one on only when configured:
  - Home Assistant: find devices, read states, call services, HA weather.
  - Some devices directly, without Home Assistant: Philips Hue lights, a Devialet
    speaker, a Google TV / Android TV device (and, with its screen read over ADB,
    finding and starting a film or an episode inside an app), an Apple TV and HomePods,
    a Nuki smart lock, Netatmo cameras and sensors, a PetKit feeder and litter box
    ([docs/home-devices.md](docs/home-devices.md)).
  - Weigh-ins from a Withings scale, said only when you ask
    ([docs/health.md](docs/health.md)).
  - Parcels on their way, noted when a shipping email is read.
  - Weather at home or anywhere (Open-Meteo, no key).
  - Outdoor activities: wind sports per spot (strength, gusts, direction), surf per beach
    (waves, period, wind effect, tides), and weather windows for everything else
    (cycling, running, hiking, skiing, climbing, golf, or just "I go out a lot").
  - Mailboxes (IMAP), calendars (CalDAV) and contacts' birthdays (CardDAV), with any
    provider that offers them: read-only by default; when you allow it, email
    drafts, flags and filing into your folders, and new calendar events. It never
    sends or deletes an email ([docs/mail-calendar.md](docs/mail-calendar.md)).
  - When to leave for an appointment (driving time from home, OpenStreetMap).
  - GitHub pull requests waiting for you, and reminders left with a coding assistant.
  - Tasks it runs by itself and sends as a push: a reminder at a time, a check every
    week, the briefing each morning; silent when there is nothing new. And, in a
    GitHub repository of yours, notes for your coding sessions (reminders, handoff
    notes), your repositories read-only, and the known flaws in their dependencies
    ([docs/tasks.md](docs/tasks.md)).
  - Your servers at a hosting provider (Hostinger, Hetzner Cloud): state, metrics,
    backups, Docker projects and their logs; a restart, a snapshot or a deployment on
    your yes ([docs/hosting.md](docs/hosting.md)).
  - Connectors: any service that has an MCP server, such as your bank or your hosting
    provider, read freely and changed only on your yes
    ([docs/connectors.md](docs/connectors.md)).
  - For people who code, off by default: coding jobs handed to Claude Code, which
    works on one of your GitHub repositories on its own and opens a pull request
    ([docs/jobs.md](docs/jobs.md)); and a nightly review of the agent's own requests
    that proposes fixes on your private copy of this repository
    ([docs/review.md](docs/review.md)).
  - Your own notes (a knowledge base in a GitHub repository), searched before it says it
    doesn't know: see [docs/knowledge.md](docs/knowledge.md).
  - File drives through rclone (Google Drive, iCloud Drive, OneDrive, Dropbox, a NAS, an
    S3 bucket and more, several at once): browse, search by name and by what the files
    say, read, and file things away without ever replacing a file: see
    [docs/drives.md](docs/drives.md).
  - Web search with any model, in a separate model request that only sees the query.
  - Documents and pictures ([docs/documents.md](docs/documents.md)): PDFs it writes
    (a letter, a statement, a summary) on A4 or US Letter, PDF forms filled, pages
    extracted and merged, diagrams drawn, animations; and, with a key of your own,
    pictures and clips from an image model, and calculations in a sandbox.
  - Memory: facts you tell it to remember and the steps of jobs that worked, in Markdown
    files you can edit (on the agent's disk, or in a GitHub repository of yours); the
    alerts you turned down; a search of your past conversations
    ([docs/memory.md](docs/memory.md)).
  - Public web pages and documents, read from site to site; never an address inside your
    network, and after private content only your country's public-service sites.
  - Sites behind your login, in a real Chrome next to the agent: you log in yourself
    from a link, it then opens pages, clicks and downloads (a certificate, a
    statement, an invoice); optionally pages as seen from another country, through
    a VPN subscription ([docs/browser.md](docs/browser.md)).
  - Investigations of a photo, a document, a link or a site: metadata (EXIF, author,
    dates), a domain's age and registrar, archives, certificates, places on
    OpenStreetMap, reverse image search links. Never to identify a private person. See
    [docs/web.md](docs/web.md) for the three.
  - Reminders created on the phone by the Siri Shortcut from the agent's `actions`.
  - If you use Apple devices, and only if you want it: your Apple Reminders read, added,
    changed and ticked (repeating ones too), your Notes searched and read, your devices
    found, through an iCloud login you type once and renew
    ([docs/apple.md](docs/apple.md)).
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
home server, a Raspberry Pi, a VPS, with or without Docker), which model (Claude with an
API key or on your Claude subscription, OpenAI,
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
| `ENGINE`, `CLAUDE_CODE_OAUTH_TOKEN` | `api`, unset | `subscription`: Claude answers through Claude Code on your own Claude plan instead of the API (`LLM_API_KEY` becomes optional). See [docs/subscription.md](docs/subscription.md). |
| `SUBSCRIPTION_MODEL`, `SUBSCRIPTION_DISPLAYS` | `LLM_MODEL`, `chat` | The model the subscription answers with, and where (`voice`, `notification`, `chat`); the API answers the rest. |
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
| `ANDROID_TV_HOST`, `ANDROID_TV_CLIENT_CERT`, `ANDROID_TV_CLIENT_KEY`, `ANDROID_TV_NAME`, `ANDROID_TV_MAX_VOLUME` | unset | A Google TV / Android TV device (`android-tv` extra); `ANDROID_TV_APPS`, `ANDROID_TV_CHANNEL_APP`, `ANDROID_TV_CHANNELS` add apps and live channels. |
| `ANDROID_TV_NAVIGATOR`, `ANDROID_TV_NAVIGATOR_MODEL`, `ANDROID_TV_NAVIGATOR_ACTIONS` | `false`, a small model, `25` | Find and start something inside an app of that device: a helper model reads the screen over ADB and presses the remote's keys ([docs/home-devices.md](docs/home-devices.md)). |
| `APPLE_DEVICES`, `APPLE_TV_COMPANION_CREDENTIALS`, `APPLE_TV_AIRPLAY_CREDENTIALS` | unset | Apple TV and HomePods (`apple` extra). |
| `NUKI_API_TOKEN`, `UNLOCK_TOTP_SECRET`, `UNLOCK_PHRASE` | unset | A Nuki smart lock; a code or secret phrase asked before it opens. |
| `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN` | unset | Netatmo cameras, weather station, thermostat. |
| `PETKIT_USERNAME`, `PETKIT_PASSWORD`, `PETKIT_REGION`, `PETKIT_ALERTS` | unset, `true` | PetKit feeder and litter box (`petkit` extra); supplies pushed through ntfy. |
| `DEPARTURE_CHECK` | `true` | `POST /v1/departure`: what is still unlocked or on when you leave. |
| `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, `WITHINGS_REFRESH_TOKEN` | unset | Weigh-ins from a Withings scale, read-only ([docs/health.md](docs/health.md)). |
| `DELIVERIES_ENABLED` | `true` | With a mailbox: parcels noted from shipping emails. |
| `WEB_SEARCH_ENABLED`, `WEB_SEARCH_BACKEND` | `true`, `auto` | Web search: `native` (Anthropic's own tool), or `duckduckgo`, `searxng`, `brave`, `tavily` with any model. |
| `WEB_SEARCH_URL`, `WEB_SEARCH_API_KEY`, `WEB_SEARCH_MODEL` | unset | SearXNG address; Brave or Tavily key; model that writes the answer. |
| `WEB_PAGES_ENABLED`, `WEB_TRUSTED_DOMAINS` | `true`, empty | Reading public web pages; sites added to the public services of `HOME_COUNTRY`, the only ones opened after private content ([docs/web.md](docs/web.md)). |
| `INVESTIGATIONS_ENABLED`, `PUBLIC_URL` | `false`, unset | Investigation tools; the agent's public address, for reverse image search. |
| `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`, `CALDAV_CALENDARS` | unset | Calendar, read-only. |
| `CALDAV_WRITE`, `CALDAV_DEFAULT_CALENDAR` | `false`, unset | Let the agent add events, and to which calendar (default: the server's). |
| `BROWSER_PUBLIC_URL` (+ `compose/browser.yml`) | unset | Remote browser for sites behind your login; `BROWSER_GATE`, `BROWSER_DOMAIN` and the `AUTH_` settings say how its page is reached: [docs/browser.md](docs/browser.md). |
| `VPN_SERVICE_PROVIDER`, `VPN_CONTROL_KEY` (+ `compose/vpn.yml`) | unset | Pages seen from another country, with any VPN provider gluetun supports. |
| `ICLOUD_APPLE_ID` | unset | Apple Reminders, Notes and Find My through iCloud's web services, after a login by hand: [docs/apple.md](docs/apple.md). |
| `ICLOUD_REMINDER_LISTS`, `ICLOUD_REMINDERS_DEFAULT_LIST` | all, the busiest | Reminders lists the agent sees, and where a new reminder goes. |
| `ICLOUD_NOTES`, `ICLOUD_FIND_MY` | `true` | Set to `false` to leave Notes or Find My out. |
| `CARDDAV_URL`, `CARDDAV_USERNAME`, `CARDDAV_PASSWORD` | unset | Contacts' birthdays in the agenda; the login defaults to CalDAV's. |
| `MAIL_ACCOUNTS` | `[]` | IMAP accounts (JSON), read-only; `"write": true` on an account allows drafts, flags and filing there. |
| `TRAVEL_ENABLED`, `TRAVEL_MARGIN_MINUTES` | `true`, `10` | When to leave (needs home). |
| `GITHUB_TOKEN` | unset | Pull requests waiting for you. |
| `GITHUB_REPOSITORIES` | `false` | Read the files of your repositories and check their dependencies for known flaws (OSV.dev); read-only. |
| `NOTES_REPO`, `ASSISTANT_REMINDERS_PATH`, `HANDOFFS_PATH` | unset, `reminders`, `handoffs` | Notes for your coding sessions: `owner/repo` and its folders ([docs/tasks.md](docs/tasks.md)). `ASSISTANT_REMINDERS_REPO` still works. |
| `NOTES_WRITE` | `false` | Let the agent leave reminders and handoff notes there and close reminders. |
| `NOTES_CRYPT_KEY` | unset | Optional: the git-crypt key of that repository (`MEMORY_CRYPT_KEY` when it also holds the memory). |
| `TASKS_STORE` | `local` | Where the tasks the agent runs by itself are kept: `local` (`DATA_DIR/tasks`), `github` (a folder of `NOTES_REPO`) or `off`. They need a chat channel or `NTFY_URL`. |
| `TASKS_PATH`, `TASKS_POLL_SECONDS` | `tasks`, `60` | The folder (in a repository: `<assistant>/tasks`) and how often it is checked. |
| `MORNING_BRIEFING_AT`, `EVENING_RECAP_AT` | unset | Send the briefing and the recap every day at that time (`07:30`). |
| `HOSTING_PROVIDER`, `HOSTING_API_TOKEN` | unset | Your servers at `hostinger` or `hetzner`: read freely, restarted, snapshotted or deployed to on your yes ([docs/hosting.md](docs/hosting.md)). |
| `MCP_SERVERS` | `[]` | Connectors: remote MCP servers (JSON), each with its login and `writes`: `off` or `confirm`. |
| `DOCUMENTS_ENABLED`, `PAPER_SIZE`, `PDF_FONT_DIR` | `true`, `auto`, DejaVu | PDFs, forms and drawings ([docs/documents.md](docs/documents.md)); paper `a4`, `letter` or by country; a folder of fonts. |
| `EXERCISE_PHOTOS_ENABLED` | `false` | Exercise photos from a public-domain bank. |
| `CODE_EXECUTION_ENABLED`, `CODE_EXECUTION_MODEL` | `false`, `LLM_MODEL` | Calculations and charts in the provider's sandbox (Anthropic only). |
| `GENERATION_GEMINI_API_KEY`, `GENERATION_OPENAI_API_KEY` | unset | Pictures and clips from Google's or OpenAI's models, billed there; models, an OpenAI-compatible address and limits in `.env.example`. |
| `KB_URL`, `KB_TOKEN` | unset | Knowledge base container (`compose/kb.yml`, with `KB_REPO` and `KB_GITHUB_TOKEN`). |
| `DRIVE_URL`, `DRIVE_PASSWORD`, `DRIVES` | unset, unset, `[]` | File drives through the rclone sidecar (`compose/drive.yml`): the drives as a JSON list of `name`, `remote`, and optionally `label`, `backend`, `read_only`, `content`. See [docs/drives.md](docs/drives.md). |
| `DRIVE_INDEX_HOURS` | `24` | How old the index of file names may get. |
| `DRIVE_TRASH_DAYS`, `DRIVE_TRASH_MAX_ITEM_MB`, `DRIVE_TRASH_MAX_TOTAL_MB` | `30`, `1024`, `5120` | The local copies of what the agent deletes from a drive. |
| `DRIVE_CONTENT`, `DRIVE_OCR`, `DRIVE_OCR_MODEL` | `true`, `off`, a small model | Search inside the files; `DRIVE_OCR=model` also reads scanned PDFs (Anthropic only). |
| `JOBS_RUNNER`, `CODER_TOKEN`, `JOBS_GITHUB_TOKEN` | `off`, unset, unset | Coding jobs: `claude_code` (the coder container of `compose/coder.yml`, on your Claude subscription) or `managed` (Anthropic Managed Agents, on the API). The other settings are in [docs/jobs.md](docs/jobs.md). They need `NTFY_URL`. |
| `REVIEW_REPO`, `REVIEW_AT`, `REVIEW_ALLOW_PUBLIC` | unset, `05:30`, `false` | Nightly review: a coding job reads the day's journal and opens a pull request on your own private copy of this repository. Read [docs/review.md](docs/review.md) first. |
| `REVIEW_LESSONS_REPO`, `REVIEW_LESSONS_PATH` | unset, `knowledge` | Optional: the review's lessons curated into your knowledge base's notes (needs the `curation` extra). |
| `NTFY_URL`, `NTFY_TOKEN` | unset | Push channel for `/v1/briefing` and for the tasks the agent runs by itself. |
| `WAHA_URL`, `WAHA_API_KEY`, `WHATSAPP_CHAT_ID` | unset | WhatsApp chat through a WAHA gateway (`compose/waha.yml`): your number as `<digits>@c.us`. Also `AGENT_WAHA_SESSION` (a number of the agent's own), `WHATSAPP_READABLE_CHATS` (groups it may sum up), `WHATSAPP_MARKER`. See [docs/chat.md](docs/chat.md). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` | unset, empty | Telegram chat through a bot of your own; only the listed private chats reach the agent. |
| `CHAT_VOTE_GRACE_SECONDS` | `5` | How long a one-tap answer in a chat can still be changed. |
| `WHISPER_MODEL`, `WHISPER_LANGUAGES` | empty, `LANGUAGE` | Voice messages sent in a chat, transcribed on this machine (`voice` extra). |
| `CONVERSATION_TTL_SECONDS` | `300` | How long a conversation is remembered, with the files sent in it and what waits for your yes. |
| `CHAT_CONVERSATION_TTL_SECONDS` | `43200` | The same for a conversation held in a chat (`"display": "chat"`). |
| `DATA_DIR` | `data` | Where the agent keeps what must survive a restart (the `agent_data` volume in Docker). |
| `JOURNAL_KEEP_DAYS` | `365` | Days the journal of requests is kept in `DATA_DIR/journal`; `0` turns it off (and the search of past conversations with it). |
| `FILES_KEEP_DAYS` | `30` | Days the files of a chat are kept in `DATA_DIR/files`, so they survive a restart; `0`: memory only. |
| `MEMORY_STORE` | `local` | Where the memory is kept: `local` (files in `DATA_DIR`), `github` (a repository) or `off`. See [docs/memory.md](docs/memory.md). |
| `MEMORY_REPO` | unset | `owner/repo` for `MEMORY_STORE=github`; `GITHUB_TOKEN` must be allowed to write to it. |
| `MEMORY_PATH`, `PROCEDURES_PATH` | `memory.md`, `procedures.md` | The two files; in a repository they default to a folder named after the assistant. |
| `MEMORY_CRYPT_KEY` | unset | Optional: the git-crypt key of a repository that encrypts that folder. |

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
`every`, `where`, `machine` and `group`. Any coding agent can write these files; with
`NOTES_WRITE` the agent leaves and closes them too. The format, the handoff notes and
the tasks that share it are in [docs/tasks.md](docs/tasks.md).

### Model providers

The agent loop, memory and tools speak one message format and call the model through a
small interface (`LLMClient` in `llm.py`). Two clients are shipped:

- **Anthropic** (`LLM_PROVIDER=anthropic`), the default; with `ENGINE=subscription`,
  Claude Code answers on your Claude plan instead, through the same tools and rules, and
  the API takes what it can't ([docs/subscription.md](docs/subscription.md));
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
A chat in WhatsApp or Telegram: [docs/chat.md](docs/chat.md).

## Security model

The guardrails are enforced in code, not only in the prompt.

- **Authentication**: every endpoint except `/health` (and a picture's short-lived link,
  below) needs the bearer token, compared in constant time.
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
- **Drives**: a file read from a drive, a folder listing or a search taints the request
  like an email does, so text hidden in a document can't make the agent copy or delete
  files. It never replaces a file (a taken name gets a numbered copy), deletes only on
  your own message, and first keeps a copy of what it deletes. A drive can be read-only,
  and rclone's API has no published port.
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
- **Opening a door**: the model can never open one by itself. `unlock_door` asks for
  proof only you have (the code of an authenticator app, or letters of a secret phrase
  at positions drawn by the code, once, with a lockout after five wrong answers); when
  no proof is set up it always waits for your go, even in a clean request. Turning a
  camera's monitoring off always waits for your go too.
- **Your own accounts**: what a lock's log, the cameras, the pet devices or the scale
  report is yours, not public: after reading it, a web search in that request waits
  for your go, so no query can carry it out. Weigh-ins are never brought up unasked
  nor put in a briefing.
- **iCloud apps**: the agent keeps a session to your iCloud account, never the
  password; reminders and notes taint a request like an email, a reminder written after
  a read waits for your go, and after a Find My read no new site opens and a search
  waits, so a position can't leave.
- **Web search**: a query is text that leaves the house. After private content was read
  the search waits for your go like a write; in any request, a query that repeats a
  number, an email address, a long token or a run of words from what the request holds
  (earlier replies, tool results) is refused; at most three searches run per request.
- **Web pages**: only public addresses open (never the home network, a container or
  cloud metadata, checked again after every redirect and on the address really reached).
  After web content, the agent follows what pages put forward, and a few sites of its own
  with a plain address (a site and a short readable path, no parameters); after private content, only your country's
  public-service sites and `WEB_TRUSTED_DOMAINS`, anything else waits for your go. A page
  that hides words for an assistant is flagged and ends free navigation.
- **Remote browser**: you type your passwords yourself, in a page only you reach (a
  login page with a passkey or a one-time code, or your Tailscale network); the
  agent never types one. Chrome reaches websites through a proxy that refuses
  private addresses. Clicks and typing only run in a request that read nothing but
  that same site; after private content it opens only public-service sites and the
  sites you logged into.
- **Investigations**: lookups go to public registries and archives, never to the site
  investigated; after private content no GPS position and no DNS query goes out. A photo
  is put online for a reverse image search only if you sent it, stripped of its metadata,
  for 15 minutes at an unguessable address (the one endpoint besides `/health` that takes
  no token).
- **Lockdown of memory**: after a tainted request, only the question, the reply and the
  names of the tools used stay in conversation memory; the private content is dropped, so
  it can't steer later requests. A request that fails is rolled back entirely. The
  journal keeps neither the reply nor the tool arguments of a tainted request.
- **Tasks**: a task is run later with nobody watching, so the agent only schedules one
  in a request that read nothing written by others (else it waits for your go, shown
  word for word), and only your own message changes or deletes one. A repeating task
  starts as a request that read others' content: it reports, and neither searches the
  web nor writes, unless it runs *fresh*, without what earlier runs read. Notes left
  in the notes repository follow the same rule, and closing or deleting one always
  asks first. Keys and tokens are refused in both.
- **Memory**: a remembered fact comes back in every later request, so nothing is
  remembered, forgotten or saved as a procedure after a read or in a reply quoting a
  message without your go, shown with every word; passwords, keys and tokens are refused.
  Facts are given to the model as data, not instructions, and are among what a web search
  query may not repeat. Saved chats and files are written readable by the agent's user
  only.
- **Servers**: the provider's token often opens the whole account, so the agent only
  calls a fixed list of paths (servers, metrics, backups, firewalls, Docker projects).
  What it reads there (logs, configs) taints the request. A restart or a snapshot runs
  only on your own clear request and waits for your go after any read; a deployment
  always waits, shown with its whole compose file and a warning for settings that give
  a container power over the host. None of it runs from a scheduled task.
- **Connectors**: reading from a connected service taints the request like an email. A
  write never runs when asked, even in a clean request: it waits for your go as above,
  shown with its exact arguments; with `writes: off` the write tools don't exist.
- **Files**: a file you send is read as data written by someone else (it taints the
  request), its name is reduced to a safe character set, and an office file that would
  expand like a zip bomb is not opened.
- **Documents and pictures**: a PDF or a drawing the agent writes is rendered on the
  server from an allowlist: only layout tags, and only images already kept in the
  conversation, so nothing in it can load a file or an address. Opening a kept file or
  a form taints the request. A prompt for an image model is checked like a search
  query, only pictures you sent or the agent made go along, and each request makes a
  few at most. Code runs in the model provider's sandbox, never on your machine.
- **Mail and calendar**: reading opens IMAP folders read-only and never marks an email
  as read; CalDAV and CardDAV are only queried. Writing is off until you turn it on
  (per mail account, and for the calendar). Even then no email is ever sent or
  deleted: a draft waits in your mail app for you to send, filing only goes to
  folders of your own, and a replaced draft goes to the trash. Flagging, filing and
  adding an event wait for your go once the request read anything; changing or
  removing a draft and creating a folder always do; filing never runs by itself in a
  scheduled task. Each saved draft is noted under the reply by the code, with a
  warning when it carries files after content written by others was read.
- **Chat channels**: only you reach the agent through one. WhatsApp reads the one
  chat you named; a Telegram bot drops everything that is not a private chat you
  listed, before reading it. A message that quotes or forwards another, a file and
  a group's messages are content written by others: what would act then waits for
  your go, shown as a choice you tick, and only what was shown can run. WhatsApp
  goes through an unofficial client (WAHA) whose volume is the account itself; the
  Telegram token is kept out of logs. Voice messages are transcribed on your
  machine. See [docs/chat.md](docs/chat.md).
- **Coding jobs** (off by default): only your own message, in a request that read
  nothing else, starts a job or writes to one; otherwise it waits for your go. The
  coder runs in a container of its own with none of the agent's secrets, and can
  touch what its GitHub token can, so scope that token. Its reports are data for the
  agent. A permission prompt of Claude Code can only be allowed by your own yes.
- **Nightly review** (off by default): it hands a day of the journal, your
  conversations, to the coding job, and so to Anthropic and to the pull request it
  opens. It only runs on a repository GitHub says is private, unless you say
  otherwise ([docs/review.md](docs/review.md)).
- **Secrets** stay in `.env` (mode 600 when written by the setup script); the setup script
  never prints them. The Siri Shortcut files hold the token: don't share them.

## Project layout

```
src/intelligent_home_agent/   app, agent loop, prompts, settings, tools/
scripts/                      setup.py (onboarding), build_shortcuts.py (Siri), mcp_login.py,
                              one login or pairing script per device that needs it
compose/                      one file per way to reach the agent (Caddy, Tailscale...)
coder/, agents/, environments/  the coding jobs' container, house rules and sandbox
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
