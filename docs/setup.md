# Setup, step by step

Ten minutes to a working assistant, then one feature at a time. Each step has a few ways
to do it: pick the one that matches what you already have. Everything can be changed later.

The short version, on a machine with Docker:

```bash
git clone https://github.com/robinalexandre/intelligent-home-agent.git
cd intelligent-home-agent
python3 scripts/setup.py        # asks steps 2 to 5, writes .env
docker compose up -d --build
curl -s localhost:8000/health
```

The wizard (`scripts/setup.py`) needs only Python 3. It asks a short series of questions
(steps 2 to 5 below), then shows one menu, "what else do you want to set up now?", where
you can pick nothing. Run it again later and choose **add**: it keeps your `.env` and asks
only about what you pick. Everything it does can also be done by hand in `.env`:
each step below gives the exact lines, [.env.example](../.env.example) lists every setting
in the same order as this page, and [settings.md](settings.md) is the reference table.

| Step | Choices |
|---|---|
| [1. Where it runs](#1-where-it-runs) | a computer or home server, a Raspberry Pi or NAS, a VPS; with Docker or plain Python |
| [2. The model](#2-the-model) | Claude (API key, or your Claude subscription), OpenAI, Mistral, Gemini, Groq, OpenRouter, a local model (Ollama, LM Studio), any OpenAI-compatible server |
| [3. The home](#3-the-home) | where you live; Home Assistant on the same network, across a VPN, through its cloud address, or no home control |
| [4. Reaching the agent](#4-reaching-the-agent) | this machine only, the home network, Tailscale, Tailscale Funnel, Cloudflare Tunnel, your own domain (Caddy, Traefik, another proxy) |
| [5. Talking to it](#5-talking-to-it) | Siri, Home Assistant Assist, Google Home, Alexa, WhatsApp, Telegram, push notifications, a scheduler, your own client |
| [6. Check, update, back up](#6-check-update-back-up) | |
| [Then add what you want](#then-add-what-you-want) | home devices, mail and calendar, files, the web, memory, tasks, connectors, and more: one line each |

## 1. Where it runs

The agent is one light Python process; the model runs at the provider, or on a machine of
yours (step 2).

| Where | Good when | Mind |
|---|---|---|
| A computer or home server, always on | Home Assistant is on the same network | Reaching it from outside the home is step 4. |
| A Raspberry Pi (4 or 5) or a NAS with Docker | You already have one running | 64-bit system; a Pi that already runs Home Assistant may be short on memory for both. |
| A VPS | You want it reachable from anywhere, with nothing exposed at home | It reaches Home Assistant through a VPN (step 3). |
| Your laptop | Trying it out | It only answers while the laptop is awake. |

**With Docker** (recommended): install [Docker](https://docs.docker.com/get-started/get-docker/)
with the Compose plugin. Every later choice that needs a container is a small file in
[`compose/`](../compose), added through `COMPOSE_FILE` in `.env`; the wizard writes that
line, so the command is always `docker compose up -d --build`.

**Without Docker**: Python 3.12 or later.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install ".[documents]"
python scripts/setup.py                # answer "n" to the Docker question
uvicorn intelligent_home_agent.main:app --host 127.0.0.1 --port 8000
```

Step 4 is then yours to set up (a reverse proxy, `tailscale serve 8000`, `cloudflared`),
and the features that are containers (knowledge base, file drives, remote browser, coding
jobs) are yours to run. [deployment.md](deployment.md#without-docker) has a systemd unit.

## 2. The model

The model must support **tool calling** (function calling): that is how the agent reads a
calendar or switches a light.

| Choice | `.env` | Notes |
|---|---|---|
| Claude, API key | `LLM_PROVIDER=anthropic`, `LLM_API_KEY=` (console.anthropic.com) | The default. `LLM_MODEL` defaults to `claude-sonnet-5-5`. |
| Claude, your Claude subscription | `LLM_PROVIDER=anthropic`, `ENGINE=subscription`, `CLAUDE_CODE_OAUTH_TOKEN=`, `EXTRAS=documents,subscription` | No API bill; a few seconds slower per reply. See below. |
| OpenAI | `LLM_PROVIDER=openai`, `LLM_API_KEY=`, `LLM_MODEL=` | |
| Mistral | `LLM_PROVIDER=mistral`, `LLM_API_KEY=`, `LLM_MODEL=` | |
| Google Gemini | `LLM_PROVIDER=gemini`, `LLM_API_KEY=`, `LLM_MODEL=` | Through Google's OpenAI-compatible endpoint. |
| Groq | `LLM_PROVIDER=groq`, `LLM_API_KEY=`, `LLM_MODEL=` | |
| OpenRouter | `LLM_PROVIDER=openrouter`, `LLM_API_KEY=`, `LLM_MODEL=vendor/model` | Many models behind one key. |
| Ollama (local) | `LLM_PROVIDER=ollama`, `LLM_MODEL=` | No key, nothing leaves the machine. See below. |
| LM Studio (local) | `LLM_PROVIDER=lmstudio`, `LLM_MODEL=` | Start LM Studio's server first. |
| Any other server | `LLM_PROVIDER=openai-compatible`, `LLM_BASE_URL=http://.../v1`, `LLM_MODEL=` | llama.cpp, vLLM, LocalAI, a company gateway. A key only if it wants one. |

Model names change often, so only Anthropic has a default: copy the name from your
provider's model list. Small local models call tools less reliably than large hosted ones;
if the agent answers without looking anything up, try a larger model.

**Claude on your Claude subscription** (Pro, Max...): Claude Code runs inside the agent,
signed in to your plan, and answers through the agent's own tools and rules. On a computer
with a browser run `claude setup-token` and paste the token (the wizard's
"claude-subscription" choice). Each reply starts a Claude Code process: fine in a chat or
for a briefing, slow for a voice assistant. It can't read a file you send; with an API
key as well, the API takes those turns and the spoken replies
(`SUBSCRIPTION_DISPLAYS`). It is your own plan for your own use: read its terms.
[subscription.md](subscription.md) has the details.

**A local model with Ollama**:

- *In Docker, next to the agent*: add `compose/ollama.yml` to `COMPOSE_FILE` (the wizard
  offers it), start, then pull the model once: `docker compose exec ollama ollama pull <model>`.
- *Ollama already on the same machine*: `LLM_BASE_URL=http://host.docker.internal:11434/v1`
  when the agent runs in Docker (Ollama must listen on all interfaces:
  `OLLAMA_HOST=0.0.0.0`); nothing to set without Docker.
- *Ollama on another machine*: `LLM_BASE_URL=http://<its address>:11434/v1`.

Web search works with every model with nothing to set: see [the web](#the-web) below.

## 3. The home

**Where you live** gives the weather, travel times and local search results. The wizard
looks the coordinates up from a town's name (it sends that name to Open-Meteo), or type
them:

```bash
HOME_CITY=Springfield
HOME_COUNTRY=US
HOME_LATITUDE=0.0
HOME_LONGITUDE=0.0
TIMEZONE=America/New_York
```

**Home control** goes through [Home Assistant](https://www.home-assistant.io/), which
knows nearly every device. Without it, skip this: the agent still answers questions, gives
the weather and the briefing, and can drive [a few devices by itself](#home-devices).

Create a token in Home Assistant: your profile (bottom left) > **Security** >
**Long-lived access tokens** > Create. Better, create a dedicated non-admin user for the
agent and make the token from that user.

| The agent is... | `HA_URL` | Also |
|---|---|---|
| on the same network as Home Assistant | `http://homeassistant.local:8123`, or `http://<its IP>:8123` if that name doesn't resolve from Docker | |
| on the Home Assistant machine itself | `http://host.docker.internal:8123` | |
| elsewhere (a VPS), both on Tailscale | `http://<HA's tailnet name or 100.x address>:8123` | Add `compose/tailscale.yml` and `TS_AUTHKEY`: the agent reaches Home Assistant through that sidecar (`HA_PROXY_URL` is set for you). On the Home Assistant side, install the Tailscale add-on. |
| elsewhere, another VPN (WireGuard, ZeroTier) | Home Assistant's address on that VPN | Set the VPN up on the host; nothing to add in `.env`. |
| elsewhere, Home Assistant Cloud (Nabu Casa) or your own HTTPS address | `https://<your remote address>` | Your Home Assistant is then reachable from the internet: keep its login protected by 2FA. |

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<the long-lived token>
```

The agent only sees common device types (`HA_EXPOSED_DOMAINS`) and asks for a yes in a
later message before acting on locks, alarms and covers (`HA_SENSITIVE_DOMAINS`). Hide
anything else with `HA_ENTITY_DENYLIST`. The [security model](../README.md#security-model)
says what the code enforces.

## 4. Reaching the agent

Clients (your phone, a speaker, Home Assistant) send HTTP requests to the agent with the
token from `.env`. Choose by where those clients are. A chat in WhatsApp or Telegram
needs none of this: the agent calls out, so "this machine only" is enough.

| Choice | Address clients use | You need | In `.env` |
|---|---|---|---|
| **This machine only** (default) | `http://127.0.0.1:8000` | nothing | nothing |
| **The home network** | `http://<machine's IP>:8000` | clients at home, on the same network | `AGENT_BIND=0.0.0.0` |
| **Tailscale** | `https://<name>.<tailnet>.ts.net` | a free Tailscale account, its app on your phone | `compose/tailscale.yml`, `TS_AUTHKEY` |
| **Tailscale Funnel** | the same address, public | Funnel allowed for the node in your tailnet policy | as above, plus `TS_SERVE=funnel` |
| **Cloudflare Tunnel** | `https://<hostname you choose>` | a domain managed by Cloudflare | `compose/cloudflared.yml`, `TUNNEL_TOKEN` |
| **Your own domain, Caddy** | `https://<your domain>` | a DNS record to this machine, ports 80 and 443 open to it | `compose/caddy.yml`, `AGENT_DOMAIN` |
| **Your own domain, Traefik** | `https://<your domain>` | a Traefik you already run with the Docker provider | `compose/traefik.yml`, `AGENT_DOMAIN` |

How to choose:

- Only voice satellites or Home Assistant at home talk to it: **the home network** is
  enough. It is plain HTTP, so never use it on a machine with a public address.
- Your phone must reach it away from home, and you are the only user: **Tailscale** keeps
  it off the public internet. The phone needs the Tailscale app connected.
- It must work from a device you can't put on a VPN (a HomePod away from home, a hosted
  scheduler): a public address, with **Funnel** or **Cloudflare Tunnel** when you can't or
  don't want to open ports at home, **Caddy** on a VPS or where you can.
- You already run a reverse proxy (nginx, a NAS's built-in one): keep the default and
  point the proxy to `127.0.0.1:8000`.

With a public address, the bearer token is the only thing between the internet and your
home: keep the generated one (43 random characters), and don't share the Siri Shortcut
files, which contain it.

The `.env` lines for each choice (the wizard writes them):

```bash
# Tailscale: enable MagicDNS and HTTPS certificates in the admin console (DNS page) first.
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:compose/tailscale.yml
TS_AUTHKEY=tskey-auth-...        # admin console > Settings > Keys
TS_HOSTNAME=home-agent           # the <name> in the address
# TS_SERVE=funnel                # public instead of tailnet-only

# Cloudflare Tunnel: Zero Trust > Networks > Tunnels > create one, add a public hostname
# whose service is http://agent:8000, copy the token.
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:compose/cloudflared.yml
TUNNEL_TOKEN=...

# Caddy
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:compose/caddy.yml
AGENT_DOMAIN=agent.example.com

# Traefik: TRAEFIK_NETWORK is the Docker network it shares with its services.
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:compose/traefik.yml
AGENT_DOMAIN=agent.example.com
TRAEFIK_NETWORK=proxy
TRAEFIK_CERTRESOLVER=letsencrypt
```

Several files combine, joined with `:`, e.g.
`COMPOSE_FILE=docker-compose.yml:compose/caddy.yml:compose/ollama.yml`.

## 5. Talking to it

Pick one or several; each is independent.

| Channel | Free questions | Briefing | What it takes |
|---|---|---|---|
| Siri (iPhone, HomePod, Mac, CarPlay) | yes | spoken or as a notification | [siri-shortcut.md](siri-shortcut.md); a Mac builds the Shortcuts for you. Needs step 4 beyond "this machine only". |
| Home Assistant Assist (voice satellites, the companion app on Android and watches) | yes | yes | [integrations.md](integrations.md#home-assistant-assist) |
| Google Home, Alexa | through a Home Assistant routine | yes | [integrations.md](integrations.md#google-home--google-assistant) |
| WhatsApp | yes, in writing, with files and voice messages | yes, and you can answer it | A WAHA gateway you run (`compose/waha.yml`; an unofficial WhatsApp client) and `WHATSAPP_CHAT_ID`: [chat.md](chat.md#whatsapp) |
| Telegram | yes, the same | yes, the same | A bot of your own, nothing else to run: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`: [chat.md](chat.md#telegram) |
| Push notifications (ntfy, any phone or desktop) | no | yes | `NTFY_URL=https://ntfy.sh/<a long random topic>`, then the ntfy app: [integrations.md](integrations.md#phone-and-desktop-notifications-ntfy) |
| A scheduler (cron, Home Assistant, a hosted one) | n/a | yes | `POST /v1/briefing`: [integrations.md](integrations.md#scheduled-tasks-cron-a-chat-or-coding-assistant-app) |
| Your own client | yes | yes | `POST /v1/ask` with `Authorization: Bearer <AGENT_API_TOKEN>`: [integrations.md](integrations.md) |

A chat or a push channel is also what lets the agent speak first: the briefing at a set
time, reminders, alerts ([tasks and alerts](#tasks-and-alerts) below).

The quickest way to a first conversation with nothing exposed: Telegram.

```bash
TELEGRAM_BOT_TOKEN=123456:AA...     # from @BotFather, /newbot
TELEGRAM_CHAT_IDS=4242              # your chat's id; the wizard reads it for you
```

## 6. Check, update, back up

```bash
docker compose ps                         # every service "running" or "healthy"
docker compose logs agent | grep tools:   # the tools that are on, from what you configured
curl -s <address>/health                  # {"status":"ok",...}
TOKEN=$(grep '^AGENT_API_TOKEN=' .env | cut -d= -f2)
curl -s <address>/health/ha -H "Authorization: Bearer $TOKEN"     # {"home_assistant":true}
curl -s <address>/v1/ask -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"text": "What can you do?"}'
```

If something is off:

| Symptom | Look at |
|---|---|
| The agent container restarts in a loop | `docker compose logs agent`: a setting is missing or wrong, and the last lines name it. |
| `home_assistant: false` | `HA_URL` from inside the container (`homeassistant.local` often doesn't resolve there: use the IP), the token, or the VPN sidecar's logs. |
| A tool you expected is missing from `tools:` | Its settings: each tool is only on when configured; the log says why a feature is off. |
| The model answers but never uses a tool | The model doesn't support tool calling, or is too small: try another. |
| `The model server answered 404` | `LLM_MODEL` isn't a model of that provider, or `LLM_BASE_URL` lacks its `/v1`. |
| The Tailscale address has no certificate | Enable HTTPS certificates in the tailnet's DNS settings, then restart the sidecar. |

**Update**: `git pull && docker compose up -d --build` (without Docker: `git pull`,
`pip install ".[documents]"` with the extras you use, restart).

**Back up** two things:

- `.env`: every secret and every choice.
- The `agent_data` volume (`DATA_DIR`, `./data` without Docker): the memory, the journal,
  the tasks, the chats and their files, renewed logins
  ([what is in it](settings.md#what-the-agent-keeps-on-disk)).

Some features keep a login in a volume of their own, which you would otherwise redo:
`waha_sessions` (WhatsApp), `drive_config` (file drives), `browser_profile` and
`browser_auth` (remote browser), `tailscale_state`, `coder_work` (coding jobs). The others
(`caddy_data`, `ollama_models`, `kb_data`) only hold what can be fetched again.

What leaves your server, and to whom: [deployment.md](deployment.md#data-that-leaves-the-server).

## Then add what you want

Everything below is off until its settings are filled in, and none of it is needed by
the rest. To add one: run `python3 scripts/setup.py` again and choose **add**, or put the
lines in `.env` by hand; then `docker compose up -d --build`. Each page starts the same
way: what it does, what you need, the settings, how to check it works, and what was and
wasn't verified against the real service.

### Home devices

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Devices without Home Assistant | Philips Hue lights, a Devialet speaker, a Google TV / Android TV device, an Apple TV and HomePods, a Nuki lock, Netatmo cameras and sensors, a PetKit feeder and litter box | each device's address or account; three of them an optional library | [home-devices.md](home-devices.md) |
| Leaving home | what is still unlocked, on or playing when you leave, fixed on your yes | a device above or Home Assistant, and a phone automation calling `POST /v1/departure` | [home-devices.md](home-devices.md#leaving-home) |
| Weigh-ins | "what do I weigh?", the trend; read-only | a Withings scale and a free developer application | [health.md](health.md) |

### Mail and calendar

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Mail | recent and searched emails; when you allow it, drafts, flags and filing (never sends, never deletes); parcels noted from shipping emails | IMAP with an app password: Gmail, iCloud, Fastmail, most hosts. Not Outlook.com / Microsoft 365 (OAuth) | [mail-calendar.md](mail-calendar.md#mail) |
| Calendar | your day, when to leave for an appointment; when you allow it, new events | CalDAV with a password: iCloud, Fastmail, Nextcloud... Not Google Calendar or Microsoft 365 (OAuth) | [mail-calendar.md](mail-calendar.md#calendar) |
| Contacts' birthdays | birthdays in the agenda | CardDAV, same login | [mail-calendar.md](mail-calendar.md#contacts-birthdays) |

### Files and documents

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| File drives | browse, search (by name and by content), read and file your documents | an rclone sidecar (`compose/drive.yml`) and a login per drive: Google Drive, iCloud Drive, OneDrive, Dropbox, a NAS, S3... | [drives.md](drives.md) |
| Documents and pictures | PDFs written, forms filled, pages extracted, drawings; on by default with Docker | nothing; a Google or OpenAI key for generated pictures; Anthropic's API for the code sandbox | [documents.md](documents.md) |

### The web

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Web search | answers from the web, on by default | nothing: Claude's own tool with Anthropic, DuckDuckGo with the others; or your SearXNG (`compose/searxng.yml`), a Brave or Tavily key | [web.md](web.md#search) |
| Web pages | public pages and documents read; on by default | nothing | [web.md](web.md#reading-pages) |
| Investigations | a photo's metadata, a domain's age, archives, places; off by default | `INVESTIGATIONS_ENABLED=true`; a public address for reverse image search | [web.md](web.md#investigations) |

### Memory and knowledge

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Memory | facts you tell it to remember, procedures that worked, a search of past conversations; on by default | nothing (files in `DATA_DIR`), or a GitHub repository of yours | [memory.md](memory.md) |
| Knowledge base | your own Markdown notes searched before it says it doesn't know | a GitHub repository of notes and `compose/kb.yml` | [knowledge.md](knowledge.md) |

### Tasks and alerts

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Tasks | "remind me tomorrow at ten", "every Monday tell me...", the briefing by itself every morning | a chat channel or `NTFY_URL` (step 5) | [tasks.md](tasks.md) |
| Notes for your coding sessions | reminders and handoff notes in a repository, pull requests waiting for you, your repositories read | a GitHub token, a notes repository | [tasks.md](tasks.md#the-notes-repository) |

### Chat channels

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| WhatsApp, Telegram | a written conversation with files, voice messages and one-tap approvals; where the agent's own messages arrive | step 5 above | [chat.md](chat.md) |
| Voice assistants, push, schedulers | Siri, Home Assistant Assist, Google Home, Alexa, ntfy, cron | step 5 above | [integrations.md](integrations.md), [siri-shortcut.md](siri-shortcut.md) |

### Connectors and hosting

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Connectors | any service with an MCP server (a bank, an accounting tool, a cloud provider): read freely, changed only on your yes | the server's address and a login | [connectors.md](connectors.md) |
| Your servers | state, metrics, backups, Docker projects; a restart or a snapshot on your yes | an API token at Hostinger or Hetzner Cloud (others: a connector) | [hosting.md](hosting.md) |

### Apple apps

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Reminders, Notes, Find My | reminders read and written, notes read, devices found | an iCloud login typed once in the agent's container, the `icloud` extra | [apple.md](apple.md) |

iCloud's calendar, contacts and mail are under [mail and calendar](#mail-and-calendar),
iCloud Drive under [file drives](#files-and-documents), an Apple TV and HomePods under
[home devices](#home-devices), Siri under step 5.

### Remote browser

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Sites behind your login | a real Chrome you log into from a link; the agent then opens pages, clicks and downloads there (a statement, an invoice) | Docker, some 700 MB of memory, and step 4 with Caddy, Traefik or Tailscale | [browser.md](browser.md) |
| Seen from another country | prices as shown to a visitor from elsewhere | the remote browser and a VPN subscription | [browser.md](browser.md#seen-from-another-country-optional) |

### Coding jobs and nightly review

For people who code; off by default.

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Coding jobs | a fix or a feature on one of your GitHub repositories, done by Claude Code, ending in a pull request | a chat or push channel, a scoped GitHub token, a Claude plan (`compose/coder.yml`) or Anthropic's API | [jobs.md](jobs.md) |
| Nightly review | a pull request that fixes what made the agent fail the day before | the coding jobs and your own private copy of this repository; read the page first | [review.md](review.md) |

### Outdoor

| Feature | Gives you | Needs | Page |
|---|---|---|---|
| Activities | the forecast the way each activity needs it: wind per spot, surf per beach, a dry window for a ride, a run, a hike | your home location; spots for wind sports and surf | [outdoor.md](outdoor.md) |
| Wind measured now | readings from beacons, airports or official stations near your spots | a spot; a free id for Météo-France | [outdoor.md](outdoor.md#wind-measured-now) |
| Alerts | a message when a good window is coming | a chat channel or `NTFY_URL` | [outdoor.md](outdoor.md#alerts) |

### Reference

[settings.md](settings.md) (every setting), [deployment.md](deployment.md) (running it,
what leaves the server), [subscription.md](subscription.md) (Claude on your plan),
[the security model](../README.md#security-model).
