# Settings

Every setting, in the order of [setup.md](setup.md) and [.env.example](../.env.example).
They are environment variables, usually written in `.env`; an empty value means "not set".
`python3 scripts/setup.py` writes the common ones. Only two are required:
`AGENT_API_TOKEN`, and `LLM_API_KEY` (unless the model is local or answers on a Claude
subscription). A feature is off until its settings are filled in, and the startup log
(`docker compose logs agent`) lists the tools that are on and says why a feature is off.

Two kinds of settings share `.env`:

- those the **agent** reads (`config.py`): most of this page;
- those **Docker Compose** reads to build and wire the containers, marked *(Compose)*:
  the agent never sees them, and they do nothing without Docker.

- [The essentials](#the-essentials): [API](#api), [Docker Compose](#docker-compose),
  [model](#the-model), [assistant](#the-assistant), [home](#the-home),
  [reaching the agent](#reaching-the-agent), [talking to it](#talking-to-it)
- Features: [home devices](#home-devices), [mail and calendar](#mail-and-calendar),
  [files and documents](#files-and-documents), [the web](#the-web),
  [memory and knowledge](#memory-and-knowledge), [tasks and alerts](#tasks-and-alerts),
  [connectors and hosting](#connectors-and-hosting), [Apple apps](#apple-apps),
  [remote browser](#remote-browser), [coding jobs and nightly review](#coding-jobs-and-nightly-review),
  [outdoor](#outdoor)

## The essentials

### API

| Setting | Default | What it does |
|---|---|---|
| `AGENT_API_TOKEN` | required | Bearer token every client sends. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `API_DOCS` | `false` | `true` serves the API's description at `/docs`, `/redoc` and `/openapi.json`. Those pages take no token: anyone who reaches the agent can read them. |

### Docker Compose

| Setting | Default | What it does |
|---|---|---|
| `COMPOSE_FILE`, `COMPOSE_PATH_SEPARATOR` *(Compose)* | `docker-compose.yml` | The files of `compose/` in use, joined with `:` ([setup.md](setup.md#4-reaching-the-agent)). |
| `EXTRAS` *(Compose)* | `documents` | Optional parts built into the image, comma-separated: `documents`, `video`, `subscription`, `voice`, `icloud`, `browser`, `devices` (or `petkit`, `android-tv`, `apple`), `curation`. Rebuild after changing it. |
| `SYSTEM_PACKAGES` *(Compose)* | empty | Debian packages added to the image, e.g. `adb` for the TV navigator. |
| `DATA_DIR` | `data` | Where the agent keeps what must survive a restart; the `agent_data` volume in Docker (leave it as it is there). See [below](#what-the-agent-keeps-on-disk). |

### The model

[setup.md, step 2](setup.md#2-the-model); [subscription.md](subscription.md).

| Setting | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, `mistral`, `gemini`, `groq`, `openrouter`, `ollama`, `lmstudio` or `openai-compatible`. |
| `LLM_API_KEY` | required, except local servers and a subscription | The provider's key (`ANTHROPIC_API_KEY` also accepted). |
| `LLM_MODEL` | `claude-sonnet-5-5` with Anthropic, else required | The model that answers; it must support tool calling (`ANTHROPIC_MODEL` also accepted). |
| `LLM_BASE_URL` | the provider's | The server's address, usually ending in `/v1`; required for `openai-compatible`. |
| `MAX_TOKENS`, `MAX_TOOL_ITERATIONS` | `1024`, `8` | Length of a spoken reply, and how many rounds of tools one request may take. |
| `MAX_CHAT_TOKENS`, `MAX_CHAT_TOOL_ITERATIONS` | `4096`, `30` | The same for a reply read in a chat (`"display": "chat"`). |
| `ENGINE` | `api` | `subscription`: Claude Code answers on your Claude plan (only with `LLM_PROVIDER=anthropic`, needs the `subscription` extra). |
| `CLAUDE_CODE_OAUTH_TOKEN` | unset | The token `claude setup-token` prints; also the coder's login for [coding jobs](jobs.md). |
| `SUBSCRIPTION_MODEL` | `LLM_MODEL` | The model the subscription answers with. |
| `SUBSCRIPTION_DISPLAYS` | `chat` | Where the subscription answers, among `voice`, `notification`, `chat`; the API answers the rest. Without an API key it answers everywhere. |

### The assistant

| Setting | Default | What it does |
|---|---|---|
| `ASSISTANT_NAME` | `Assistant` | What the assistant calls itself; also closes a conversation ("thanks, Assistant"). |
| `USER_NAME` | unset | What the assistant calls you. |
| `LANGUAGE` | `en` | `en` or `fr`: the replies and the words the code itself writes. |
| `TIMEZONE` | `UTC` | IANA time zone, e.g. `Europe/Paris`. |
| `CONVERSATION_TTL_SECONDS` | `300` | How long a spoken conversation is remembered, with its files and what waits for your yes. |
| `CHAT_CONVERSATION_TTL_SECONDS` | `43200` | The same for a chat (`"display": "chat"`). |
| `MAX_HISTORY_MESSAGES` | `40` | How many messages of a conversation the model is given again. |

### The home

| Setting | Default | What it does |
|---|---|---|
| `HOME_LATITUDE`, `HOME_LONGITUDE` | unset | Home, for weather, travel time and outdoor forecasts. |
| `HOME_CITY`, `HOME_COUNTRY` | unset | Local web search results; the two-letter country also picks the paper size and the public-service sites ([web.md](web.md)). |
| `HA_URL`, `HA_TOKEN` | `http://homeassistant.local:8123`, unset | Home Assistant; no token means no home tools. |
| `HA_EXPOSED_DOMAINS` | common domains | Only entities of these domains are visible. |
| `HA_SENSITIVE_DOMAINS` | `lock,alarm_control_panel,cover` | Acting on these needs your yes in a later message. |
| `HA_ENTITY_DENYLIST` | empty | Entities the agent never sees. |
| `HA_PROXY_URL` | unset | HTTP proxy to reach Home Assistant (a VPN sidecar; `compose/tailscale.yml` sets it). |
| `HA_TIMEOUT_SECONDS` | `10` | How long a call to Home Assistant may take. |

### Reaching the agent

All *(Compose)*; [setup.md, step 4](setup.md#4-reaching-the-agent).

| Setting | Default | What it does |
|---|---|---|
| `AGENT_BIND` | `127.0.0.1` | `0.0.0.0` opens port 8000 to the home network, in plain HTTP. |
| `AGENT_DOMAIN` | unset | The agent's domain name, with `compose/caddy.yml` or `compose/traefik.yml`. |
| `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `TRAEFIK_CERTRESOLVER` | `proxy`, `websecure`, `letsencrypt` | Your Traefik's Docker network, HTTPS entry point and certificate resolver. |
| `TS_AUTHKEY`, `TS_HOSTNAME` | required, `home-agent` | Tailscale: an auth key and the node's name (`compose/tailscale.yml`). |
| `TS_SERVE` | `serve` | `funnel`: a public address. `serve-browser`: also the remote browser's page, tailnet only. |
| `TS_EXTRA_ARGS` | empty | e.g. `--accept-routes`, to reach devices behind a subnet router. |
| `TUNNEL_TOKEN` | required | Cloudflare Tunnel's token (`compose/cloudflared.yml`). |

### Talking to it

[setup.md, step 5](setup.md#5-talking-to-it); [chat.md](chat.md), [integrations.md](integrations.md).

| Setting | Default | What it does |
|---|---|---|
| `NTFY_URL`, `NTFY_TOKEN` | unset | Push channel (a full ntfy topic URL; a token for a protected server): `/v1/briefing` with `push`, and what the agent says by itself when there is no chat. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` | unset, empty | Telegram chat through a bot of your own; only the listed private chats reach the agent. On with both. |
| `TELEGRAM_API_URL` | `https://api.telegram.org` | Another server speaking the Bot API. |
| `WAHA_URL`, `WAHA_API_KEY`, `WAHA_SESSION` | unset, unset, `default` | The WAHA gateway linked to your WhatsApp number (`compose/waha.yml` sets the URL). |
| `WAHA_DASHBOARD_PASSWORD`, `WAHA_DASHBOARD_USERNAME`, `WAHA_ENGINE`, `WAHA_PORT` *(Compose)* | required, `admin`, `GOWS`, `3000` | The WAHA container: its dashboard's login, its engine, its port on this machine's loopback. |
| `WHATSAPP_CHAT_ID` | unset | Your number as `<digits>@c.us`: the one chat the agent talks in. WhatsApp is on with this and WAHA. |
| `AGENT_WAHA_SESSION`, `AGENT_WAHA_URL`, `AGENT_WAHA_API_KEY` | unset | A session linked to a number of the agent's own; URL and key default to `WAHA_*`. |
| `WHATSAPP_MARKER` | `🤖` | What the agent's WhatsApp messages start with. |
| `WHATSAPP_POLL_SECONDS` | `5` | How often the chat is read. |
| `WHATSAPP_READABLE_CHATS` | empty | Groups and communities the agent may sum up on request. |
| `CHAT_VOTE_GRACE_SECONDS` | `5` | How long a one-tap answer in a chat can still be changed. |
| `WHISPER_MODEL`, `WHISPER_LANGUAGES` | empty, `LANGUAGE` | Voice messages sent in a chat, transcribed on this machine (`voice` extra); the languages you speak in them. |
| `BRIEF_SECTIONS` | all | What the briefing and the recap cover, any of `calendar,travel,mail,pull_requests,reminders,weather,activities`. A section without its source is skipped. |
| `WEATHER_DETAIL` | `short` | `off`, `short` (one sentence) or `detailed`. |

## Features

### Home devices

[home-devices.md](home-devices.md), [health.md](health.md).

| Setting | Default | What it does |
|---|---|---|
| `HUE_BRIDGE_URL`, `HUE_APP_KEY` | unset | Philips Hue lights, straight from the bridge. |
| `DEVIALET_URL`, `DEVIALET_MAX_VOLUME` | unset, `70` | A Devialet Phantom speaker; every volume the agent sets is capped. |
| `ANDROID_TV_HOST`, `ANDROID_TV_CLIENT_CERT`, `ANDROID_TV_CLIENT_KEY` | unset | A Google TV / Android TV device (`android-tv` extra); `scripts/android_tv_pair.py` writes them. |
| `ANDROID_TV_NAME`, `ANDROID_TV_MAX_VOLUME` | `TV`, `70` | What you call it, and the volume cap. |
| `ANDROID_TV_APPS`, `ANDROID_TV_CHANNEL_APP`, `ANDROID_TV_CHANNELS` | built-in apps, unset, empty | More apps (JSON), the live TV app, and names for channel numbers (JSON). |
| `ANDROID_TV_NAVIGATOR`, `ANDROID_TV_NAVIGATOR_MODEL`, `ANDROID_TV_NAVIGATOR_ACTIONS` | `false`, a small model, `25` | Find and start something inside an app: a helper model reads the screen over ADB. |
| `APPLE_DEVICES`, `APPLE_TV_COMPANION_CREDENTIALS`, `APPLE_TV_AIRPLAY_CREDENTIALS` | unset | Apple TV and HomePods (`apple` extra); what `scripts/apple_devices.py` prints. |
| `NUKI_API_TOKEN` | unset | A Nuki smart lock, through the Nuki Web API. |
| `UNLOCK_TOTP_SECRET`, `UNLOCK_PHRASE` | unset | Proof asked before a door opens (`scripts/unlock_codes_setup.py`). Neither: opening waits for your yes each time. |
| `CAR_PROVIDER`, `CAR_VIN`, `CAR_REFRESH_TOKEN`, `CAR_CLIENT_ID` | unset | A connected car: `audi` (myAudi, through the app's unofficial API) so far; `scripts/audi_login.py` writes them. |
| `CAR_PIN`, `CAR_NAME` | unset, "the car" | The maker's PIN, which allows locking and the heating (unlocking also needs the unlock proof above); what you call the car. |
| `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN` | unset | Netatmo cameras, weather station, thermostat (`scripts/netatmo_login.py`). |
| `PETKIT_USERNAME`, `PETKIT_PASSWORD`, `PETKIT_REGION` | unset, unset, `HOME_COUNTRY` | PetKit feeder and litter box (`petkit` extra), through a second PetKit account. |
| `PETKIT_ALERTS` | `true` | With a chat or `NTFY_URL`: a message when supplies run low or the bin is full. |
| `PETKIT_LITTER_ALERT_PERCENT`, `PETKIT_FOOD_ALERT_PERCENT` | `50`, `45` | The levels under which you are told. |
| `PETKIT_BIN_REMIND_MINUTES`, `PETKIT_CHECK_MINUTES` | `120`, `180` | How often a full bin is said again, and how often the supplies are looked at. |
| `PETKIT_QUIET_START`, `PETKIT_QUIET_END` | `22`, `8` | Hours between which nothing is read or sent. |
| `HOME_PROXY_URL` | `HA_PROXY_URL` | HTTP proxy to reach devices on the home network from elsewhere. |
| `HOME_TIMEOUT_SECONDS` | `10` | How long a call to a device may take. |
| `DEPARTURE_CHECK` | `true` | `POST /v1/departure`: what is still unlocked or on when you leave. |
| `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`, `WITHINGS_REFRESH_TOKEN` | unset | Weigh-ins from a Withings scale, read-only (`scripts/withings_login.py`). |

### Mail and calendar

[mail-calendar.md](mail-calendar.md).

| Setting | Default | What it does |
|---|---|---|
| `MAIL_ACCOUNTS` | `[]` | IMAP accounts, a JSON list of `name`, `host`, `username`, `password`; optional `port` (993), `mailbox`, `write` (drafts, flags, filing), `address`, `drafts`, `sent`, `trash`. |
| `DELIVERIES_ENABLED` | `true` | With a mailbox: parcels noted from shipping emails. |
| `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` | unset | The calendar server and an app password. |
| `CALDAV_CALENDARS` | all | Only these calendars, by name. |
| `CALDAV_WRITE`, `CALDAV_DEFAULT_CALENDAR` | `false`, the server's | Let the agent add events, and to which calendar. |
| `CARDDAV_URL`, `CARDDAV_USERNAME`, `CARDDAV_PASSWORD` | unset, CalDAV's | Contacts' birthdays in the agenda. |
| `TRAVEL_ENABLED`, `TRAVEL_MARGIN_MINUTES` | `true`, `10` | When to leave for an appointment (needs home). |
| `NOMINATIM_URL`, `OSRM_URL` | OpenStreetMap's public servers | The geocoder and the router behind travel times, if you run your own. |

### Files and documents

[drives.md](drives.md), [documents.md](documents.md).

| Setting | Default | What it does |
|---|---|---|
| `DRIVE_URL`, `DRIVE_USER`, `DRIVE_PASSWORD` | set by `compose/drive.yml`, `agent`, unset | The rclone sidecar's API and the login both share. |
| `DRIVES` | `[]` | The drives, a JSON list of `name`, `remote`, and optionally `label`, `backend`, `read_only`, `content`. |
| `DRIVE_INDEX_HOURS` | `24` | How old the index of file names may get. |
| `DRIVE_TRASH_DAYS`, `DRIVE_TRASH_MAX_ITEM_MB`, `DRIVE_TRASH_MAX_TOTAL_MB` | `30`, `1024`, `5120` | The local copies of what the agent deletes from a drive. |
| `DRIVE_CONTENT` | `true` | Search inside the files. |
| `DRIVE_OCR`, `DRIVE_OCR_MODEL` | `off`, a small model | `model`: scanned PDFs read by the model (Anthropic's API only today). |
| `DOCUMENTS_ENABLED` | `true` | PDFs, forms, pages, drawings (the `documents` extra). |
| `PAPER_SIZE` | `auto` | `a4`, `letter`, or by `HOME_COUNTRY`. |
| `PDF_FONT_DIR` | DejaVu Sans | A folder of fonts for PDFs and drawings. |
| `EXERCISE_PHOTOS_ENABLED` | `false` | Exercise photos from a public-domain bank. |
| `CODE_EXECUTION_ENABLED`, `CODE_EXECUTION_MODEL` | `false`, `LLM_MODEL` | Calculations and charts in the provider's sandbox (Anthropic's API only). |
| `GENERATION_GEMINI_API_KEY`, `GENERATION_GEMINI_IMAGE_MODEL`, `GENERATION_GEMINI_VIDEO_MODEL` | unset, `gemini-2.5-flash-image`, `veo-3.1-lite-generate-001` | Pictures and clips from Google's models, billed there. |
| `GENERATION_OPENAI_API_KEY`, `GENERATION_OPENAI_BASE_URL`, `GENERATION_OPENAI_IMAGE_MODEL`, `GENERATION_OPENAI_VIDEO_MODEL` | unset, OpenAI's, `gpt-image-2`, `sora-2` | The same from OpenAI, or any server that speaks its images API. A model set to `off`: none of that kind. |
| `GENERATION_DEFAULT_PROVIDER` | the first configured | `gemini` or `openai`, when both are set. |
| `GENERATION_MAX_IMAGES`, `GENERATION_MAX_VIDEOS` | `6`, `2` | The most one request may make. |

### The web

[web.md](web.md).

| Setting | Default | What it does |
|---|---|---|
| `WEB_SEARCH_ENABLED` | `true` | Web search. |
| `WEB_SEARCH_BACKEND` | `auto` | `native` (Anthropic's own tool), or `duckduckgo`, `searxng`, `brave`, `tavily` with any model. `auto`: `native` with Anthropic, `duckduckgo` otherwise. |
| `WEB_SEARCH_URL`, `WEB_SEARCH_API_KEY` | unset | Your SearXNG's address; a Brave or Tavily key. |
| `SEARXNG_SECRET` *(Compose)* | required with `compose/searxng.yml` | The secret of the SearXNG container. |
| `WEB_SEARCH_MODEL` | a small Claude model with `native`, else `LLM_MODEL` | The model that writes the answer. |
| `WEB_SEARCH_MAX_USES`, `WEB_SEARCH_RESULTS` | `3`, `5` | Searches per question (`native`); results read per search (the others). |
| `WEB_PAGES_ENABLED` | `true` | Reading public pages and documents. |
| `WEB_TRUSTED_DOMAINS` | empty | Sites added to your country's public services, the only ones opened after private content. |
| `INVESTIGATIONS_ENABLED` | `false` | The investigation tools. |
| `PUBLIC_URL` | unset | The agent's own public address, for reverse image search. |

### Memory and knowledge

[memory.md](memory.md), [knowledge.md](knowledge.md).

| Setting | Default | What it does |
|---|---|---|
| `MEMORY_STORE` | `local` | Where the memory is kept: `local` (files in `DATA_DIR`), `github` (a repository) or `off`. |
| `MEMORY_REPO` | unset | `owner/repo` for `MEMORY_STORE=github`; `GITHUB_TOKEN` must be allowed to write to it. |
| `MEMORY_PATH`, `PROCEDURES_PATH` | `memory.md`, `procedures.md` | The two files; in a repository they default to a folder named after the assistant. |
| `MEMORY_CRYPT_KEY` | unset | The git-crypt key of a repository that encrypts that folder. |
| `JOURNAL_KEEP_DAYS` | `365` | Days the journal of requests is kept; `0` turns it off, and the search of past conversations with it. |
| `FILES_KEEP_DAYS` | `30` | Days the files of a chat are kept on disk; `0`: memory only. |
| `KB_URL`, `KB_TOKEN` | set by `compose/kb.yml`, unset | The knowledge base container and the secret both share. |
| `KB_REPO`, `KB_GITHUB_TOKEN`, `KB_PULL_SECONDS`, `KB_CRYPT_KEY` *(Compose)* | required, unset, `300`, unset | The notes repository, a token that reads it, how often it is pulled, its git-crypt key if it has one. |

#### What the agent keeps on disk

Everything is under `DATA_DIR`, readable by the agent's user only:

| Path | Holds |
|---|---|
| `memory.md`, `procedures.md` | The memory (unless `MEMORY_STORE=github`). |
| `journal/` | One line per request, one file per day. |
| `conversations/`, `files/` | Chats in progress and their files. |
| `tasks/`, `tasks-done.json`, `tasks-reports.json`, `task-outbox.json` | Tasks (unless `TASKS_STORE=github`), which ran, their reports. |
| `turned-down-alerts.json`, `activity-alerts.json`, `pet-alerts.json` | Alerts you turned down or asked for. |
| `jobs.json`, `progress-durations.json`, `review-done.json`, `coder-sessions.json` | Coding jobs, the coder's sessions and the nightly review. |
| `car.json` | The car's renewed login. |
| `icloud-web/`, `whisper/`, `claude/`, `drive-trash/` | The iCloud session, the speech model, Claude Code's transcripts, copies of files deleted from a drive. |

### Tasks and alerts

[tasks.md](tasks.md).

| Setting | Default | What it does |
|---|---|---|
| `TASKS_STORE` | `local` | Where tasks are kept: `local` (`DATA_DIR/tasks`), `github` (a folder of `NOTES_REPO`) or `off`. Tasks need a chat channel or `NTFY_URL`. |
| `TASKS_PATH`, `TASKS_POLL_SECONDS` | `tasks`, `60` | The folder (in a repository: `<assistant>/tasks`) and how often it is checked. |
| `MORNING_BRIEFING_AT`, `EVENING_RECAP_AT` | unset | Send the briefing and the recap every day at that time (`07:30`). |
| `GITHUB_TOKEN` | unset | Pull requests waiting for you; also what `MEMORY_STORE=github` and the notes repository use. |
| `GITHUB_REPOSITORIES` | `false` | Read the files of your repositories and check their dependencies for known flaws; read-only. |
| `NOTES_REPO` | unset | `owner/repo` with notes for your coding sessions (`ASSISTANT_REMINDERS_REPO` still works). |
| `ASSISTANT_REMINDERS_PATH`, `HANDOFFS_PATH` | `reminders`, `handoffs` | Its two folders. |
| `NOTES_WRITE` | `false` | Let the agent leave reminders and handoff notes there and close reminders. |
| `NOTES_CRYPT_KEY` | `MEMORY_CRYPT_KEY` when the same repository | The git-crypt key of that repository. |

### Connectors and hosting

[connectors.md](connectors.md), [hosting.md](hosting.md).

| Setting | Default | What it does |
|---|---|---|
| `MCP_SERVERS` | `[]` | Remote MCP servers, a JSON list of `name`, `url`, a login, and optionally `label`, `writes` (`off` or `confirm`), `read_tools`. |
| `HOSTING_PROVIDER`, `HOSTING_API_TOKEN` | unset | Your servers at `hostinger` or `hetzner`. |

### Apple apps

[apple.md](apple.md).

| Setting | Default | What it does |
|---|---|---|
| `ICLOUD_APPLE_ID` | unset | Apple Reminders, Notes and Find My, after a login by hand (`icloud` extra). |
| `ICLOUD_REMINDER_LISTS`, `ICLOUD_REMINDERS_DEFAULT_LIST` | all, the busiest | The Reminders lists the agent sees, and where a new reminder goes. |
| `ICLOUD_NOTES`, `ICLOUD_FIND_MY` | `true` | `false` leaves Notes or Find My out. |

### Remote browser

[browser.md](browser.md). Docker only.

| Setting | Default | What it does |
|---|---|---|
| `BROWSER_PUBLIC_URL` | unset | The address of the browser's page, sent to you as the login link. Empty: no browser. |
| `BROWSER_CDP_HOST`, `BROWSER_CDP_PORT`, `BROWSER_DOWNLOADS_DIR` | set by `compose/browser.yml` | Chrome's control port and its downloads folder. |
| `BROWSER_GATE` *(Compose)* | `login` | `tailnet`: the page is served to your Tailscale devices only, with no login of its own. |
| `BROWSER_DOMAIN`, `AUTH_DOMAIN`, `AUTH_COOKIE_DOMAIN` *(Compose)* | required with the login page | The page's name, its login page's, and the domain both are under. |
| `AUTH_SESSION_SECRET`, `AUTH_STORAGE_KEY`, `AUTH_JWT_SECRET` *(Compose)* | required with the login page | Three secrets of the login page (`openssl rand -hex 32` each). |
| `VPN_SERVICE_PROVIDER`, `VPN_TYPE` *(Compose)* | required with `compose/vpn.yml`, `wireguard` | Pages seen from another country: gluetun's name for your VPN provider, and `wireguard` or `openvpn`. |
| `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `WIREGUARD_PRESHARED_KEY`, `OPENVPN_USER`, `OPENVPN_PASSWORD` *(Compose)* | unset | The credentials your VPN provider uses. |
| `VPN_CONTROL_KEY` | unset | A key for the VPN container's control server. Without it `browser_open` takes no country. |
| `VPN_CONTROL_URL`, `BROWSER_ABROAD_PROXY` | set by `compose/vpn.yml` | That control server, and the proxy port that leaves through the VPN. |

### Coding jobs and nightly review

[jobs.md](jobs.md), [review.md](review.md).

| Setting | Default | What it does |
|---|---|---|
| `JOBS_RUNNER` | `off` | `claude_code` (the coder container, on your Claude subscription) or `managed` (Anthropic Managed Agents, on the API). Jobs need a chat channel or `NTFY_URL`. |
| `CODER_URL`, `CODER_TOKEN` | set by `compose/coder.yml`, unset | `claude_code`: where the coder is, and the secret both share. |
| `JOBS_GITHUB_TOKEN` | unset | The coder's GitHub token: its scope is what a job can touch. |
| `JOBS_GIT_NAME`, `JOBS_GIT_EMAIL` | unset | The identity of the coder's commits. |
| `JOBS_MODEL_SIMPLE`, `JOBS_MODEL_NORMAL`, `JOBS_MODEL_COMPLEX` | `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-1` | `claude_code`: the coder's model by complexity. |
| `CODER_MAX_BUDGET_USD`, `CODER_RC_NAME`, `CODER_SYNC_REPOS` *(Compose)* | `15`, `Home agent code`, `false` | `claude_code`: runaway guard per job; Remote Control's name and whether repositories are kept cloned. |
| `CODER_SESSIONS_WATCH`, `CODER_SESSIONS_POLL_SECONDS` | `true`, `60` | `claude_code`: news of every Claude Code session of the coder (one finished, one asks something), and how often they are looked at. |
| `CODER_RELAY_MODEL` *(Compose)* | `claude-sonnet-5-5` | `claude_code`: the model of the short turn that passes your answer to a session. |
| `JOBS_AGENT_ID`, `JOBS_ENVIRONMENT_ID`, `JOBS_VAULT_ID` | unset | `managed`: written by `scripts/jobs_setup.py`. |
| `JOBS_BUDGET_USD`, `JOBS_MAX_ITERATIONS`, `JOBS_WORKSPACE` | `10`, `5`, `default` | `managed`: the spend cap per job, the grader's rounds, the Anthropic workspace. |
| `REVIEW_REPO` | unset | Your own private copy of this repository, `owner/repo`. Unset: no review. |
| `REVIEW_AT`, `REVIEW_ALLOW_PUBLIC` | `05:30`, `false` | When the day before is reviewed; whether a repository that is not private may receive the journal. |
| `REVIEW_LESSONS_REPO`, `REVIEW_LESSONS_PATH` | unset, `knowledge` | Where the review's lessons are curated into notes (`curation` extra). |

### Outdoor

[outdoor.md](outdoor.md).

| Setting | Default | What it does |
|---|---|---|
| `OUTDOOR_ACTIVITIES` | none | e.g. `surf,wingfoil,cycling`; names or aliases from `activities.py`. |
| `WIND_SPOTS`, `SURF_SPOTS` | `[]` | Spots for wind sports and surf (JSON). |
| `WIND_MODEL` | `best_match` | Open-Meteo's model for wind forecasts. |
| `HOME_MOVES` | `[]` | Where home is from a given day, when you move (JSON). |
| `LIVE_WIND_SOURCES`, `LIVE_WIND_RADIUS_KM` | none, `20` | Wind measured now near the spots: any of `openwindmap`, `metar`, `meteofrance`. |
| `METEOFRANCE_APPLICATION_ID` | unset | The free id `meteofrance` needs. |
| `ACTIVITY_ALERTS` | none | Activities you are told about when a good window is coming. They need a chat or `NTFY_URL`. |
| `ACTIVITY_ALERTS_LEAD_HOURS`, `ACTIVITY_ALERTS_QUIET_START`, `ACTIVITY_ALERTS_QUIET_END` | `24`, `22`, `8` | How early at most, and the silent hours. |
| `OPEN_METEO_TIMEOUT_SECONDS` | `10` | How long a forecast request may take. |
