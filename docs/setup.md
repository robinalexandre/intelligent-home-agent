# Setup, step by step

Six steps, each with a few ways to do it. Pick the one that matches what you already have;
everything can be changed later by editing `.env` or running the wizard again.

The short version, on a machine with Docker:

```bash
git clone https://github.com/robinalexandre/intelligent-home-agent.git
cd intelligent-home-agent
python3 scripts/setup.py        # asks the questions below, writes .env
docker compose up -d --build
curl -s localhost:8000/health
```

The wizard (`scripts/setup.py`) needs only Python 3 and asks about steps 2 to 5. The rest
of this page explains each choice, and how to make it by hand in `.env`
([.env.example](../.env.example) lists every setting).

| Step | Choices |
|---|---|
| [1. Where it runs](#1-where-it-runs) | a computer or home server, a Raspberry Pi or NAS, a VPS; with Docker or plain Python |
| [2. The model](#2-the-model) | Claude, OpenAI, Mistral, Gemini, Groq, OpenRouter, a local model (Ollama, LM Studio), any OpenAI-compatible server |
| [3. The home](#3-the-home) | Home Assistant on the same network, across a VPN, through its cloud address, or no home control |
| [4. Reaching the agent](#4-reaching-the-agent) | this machine only, the home network, Tailscale, Tailscale Funnel, Cloudflare Tunnel, your own domain (Caddy, Traefik, another proxy) |
| [5. Talking to it](#5-talking-to-it) | Siri, Home Assistant Assist, Google Home, Alexa, push notifications, a scheduler, your own client |
| [6. Check, update, back up](#6-check-update-back-up) | |

## 1. Where it runs

The agent is one light Python process: the model runs at the provider (or on a machine of
yours, see step 2). Any of these works:

| Where | Good when | Mind |
|---|---|---|
| A computer or home server, always on | Home Assistant is on the same network | Reaching it from outside the home is step 4. |
| A Raspberry Pi (4 or 5) or a NAS with Docker | You already have one running | 64-bit system; a Pi that already runs Home Assistant may be short on memory for both. |
| A VPS | You want it reachable from anywhere, with nothing exposed at home | It reaches Home Assistant through a VPN (step 3). |
| Your laptop | Trying it out | It only answers while the laptop is awake. |

**With Docker** (recommended): install [Docker](https://docs.docker.com/get-started/get-docker/)
with the Compose plugin, then `docker compose up -d --build`. Every choice of steps 2 to 4
is a small file in [`compose/`](../compose) added through `COMPOSE_FILE` in `.env`; the
wizard writes that line, so the command stays the same.

**Without Docker**: Python 3.12 or later.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install .
python scripts/setup.py                # answer "n" to the Docker question
uvicorn intelligent_home_agent.main:app --host 127.0.0.1 --port 8000
```

To keep it running on Linux, a systemd unit (`/etc/systemd/system/home-agent.service`,
then `systemctl enable --now home-agent`):

```ini
[Unit]
Description=intelligent-home-agent
After=network-online.target

[Service]
User=agent
WorkingDirectory=/opt/intelligent-home-agent
ExecStart=/opt/intelligent-home-agent/.venv/bin/uvicorn intelligent_home_agent.main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Without Docker, step 4 is yours to set up (a reverse proxy, `tailscale serve 8000`,
`cloudflared`): the files in `compose/` show what each one needs.

## 2. The model

The model must support **tool calling** (also called function calling): that is how the
agent reads the calendar or switches a light. Set three values in `.env`:

| `LLM_PROVIDER` | `LLM_API_KEY` | `LLM_MODEL` | Notes |
|---|---|---|---|
| `anthropic` (default) | from console.anthropic.com | `claude-sonnet-5-5` by default | Searches the web with its own tool. |
| `openai` | from platform.openai.com | required | |
| `mistral` | from console.mistral.ai | required | |
| `gemini` | from aistudio.google.com | required | Through Google's OpenAI-compatible endpoint. |
| `groq` | from console.groq.com | required | |
| `openrouter` | from openrouter.ai | required, e.g. `vendor/model` | Many models behind one key. |
| `ollama` | none | required | Local. See below. |
| `lmstudio` | none | required | Local. Start LM Studio's server first. |
| `openai-compatible` | if the server wants one | required | Any other server: set `LLM_BASE_URL`, usually ending in `/v1` (llama.cpp, vLLM, LocalAI, a company gateway). |

Model names change often, so only Anthropic has a default: copy the name from your
provider's model list. Small local models call tools less reliably than large hosted ones;
if the agent answers without looking anything up, try a larger model.

**A local model with Ollama**, so that nothing leaves your machine:

- *In Docker, next to the agent*: add `compose/ollama.yml` (the wizard offers it), start,
  then pull the model once: `docker compose exec ollama ollama pull <model>`.
- *Ollama already installed on the same machine*: `LLM_BASE_URL=http://host.docker.internal:11434/v1`
  when the agent runs in Docker (Ollama must listen on all interfaces:
  `OLLAMA_HOST=0.0.0.0`), nothing to set without Docker.
- *Ollama on another machine*: `LLM_BASE_URL=http://<its address>:11434/v1`.

**Web search** works with every model. `WEB_SEARCH_BACKEND` says who fetches the results:

| `WEB_SEARCH_BACKEND` | Needs | Notes |
|---|---|---|
| `auto` (default) | nothing | `native` with Anthropic, `duckduckgo` with the others. |
| `native` | `LLM_PROVIDER=anthropic` | Anthropic's own search tool. |
| `duckduckgo` | nothing | No account. Best effort: it reads a web page that may change, and refuses many searches in a row. |
| `searxng` | `WEB_SEARCH_URL`, or `compose/searxng.yml` and `SEARXNG_SECRET` | Your own search engine, no account anywhere. The dependable free choice. |
| `brave` | `WEB_SEARCH_API_KEY` from brave.com/search/api | |
| `tavily` | `WEB_SEARCH_API_KEY` from tavily.com | |

With the last four, the agent fetches the results and your model writes a short answer
from them, in a request of its own that sees nothing else. `WEB_SEARCH_ENABLED=false`
turns search off.

## 3. The home

Home control goes through [Home Assistant](https://www.home-assistant.io/). Without it,
skip this step: the agent still answers questions, gives the weather, the briefing, the
calendar and the rest.

Create a token in Home Assistant: your profile (bottom left) > **Security** >
**Long-lived access tokens** > Create. Better, create a dedicated non-admin user for the
agent and make the token from that user. Then, depending on where the agent runs:

| The agent is... | `HA_URL` | Also |
|---|---|---|
| on the same network as Home Assistant | `http://homeassistant.local:8123`, or `http://<its IP>:8123` if that name doesn't resolve from Docker | |
| on the Home Assistant machine itself | `http://host.docker.internal:8123` | |
| elsewhere (a VPS), both on Tailscale | `http://<HA's tailnet name or 100.x address>:8123` | Add `compose/tailscale.yml` and `TS_AUTHKEY`: the agent reaches Home Assistant through that sidecar (`HA_PROXY_URL` is set for you). On the Home Assistant side, install the Tailscale add-on. |
| elsewhere, another VPN (WireGuard, ZeroTier) | Home Assistant's address on that VPN | Set the VPN up on the host; nothing to add in `.env`. |
| elsewhere, Home Assistant Cloud (Nabu Casa) or your own HTTPS address | `https://<your remote address>` | Your Home Assistant is then reachable from the internet: keep its login protected by 2FA. |

`.env`:

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<the long-lived token>
```

By default the agent only sees common device types (`HA_EXPOSED_DOMAINS`), and asks for a
spoken yes in a later message before acting on locks, alarms and covers
(`HA_SENSITIVE_DOMAINS`). Hide anything else with `HA_ENTITY_DENYLIST`. The
[security model](../README.md#security-model) explains what the code enforces.

## 4. Reaching the agent

Clients (your phone, a speaker, Home Assistant) send HTTPS requests to the agent with the
token from `.env`. Choose by where those clients are:

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
- You already run a reverse proxy (nginx, Traefik, a NAS's built-in one): keep the default
  and point the proxy to `127.0.0.1:8000`.

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

Several files combine: `COMPOSE_FILE=docker-compose.yml:compose/caddy.yml:compose/ollama.yml`.

## 5. Talking to it

| Channel | Free questions | Briefing | Setup |
|---|---|---|---|
| Siri (iPhone, HomePod, Mac, CarPlay) | yes | spoken or as a notification | [siri-shortcut.md](siri-shortcut.md); a Mac builds the Shortcuts for you |
| Home Assistant Assist (voice satellites, the companion app on Android and watches) | yes | yes | [integrations.md](integrations.md#home-assistant-assist) |
| Google Home, Alexa | through a Home Assistant routine | yes | [integrations.md](integrations.md) |
| Push notifications (ntfy, any phone or desktop) | no | yes | `NTFY_URL` in `.env`, then the ntfy app |
| A scheduler (cron, Home Assistant, a hosted one) | n/a | yes | `POST /v1/briefing`, [integrations.md](integrations.md) |
| Your own client | yes | yes | `POST /v1/ask` with `Authorization: Bearer <AGENT_API_TOKEN>` |

Optional data sources, each off until its settings are filled in:

| Source | Works with | Doesn't |
|---|---|---|
| Calendar (CalDAV, read-only) | iCloud (`https://caldav.icloud.com`, an app-specific password), Fastmail (`https://caldav.fastmail.com/dav/`), Nextcloud (`https://<host>/remote.php/dav`), any CalDAV server with a password | Google Calendar and Microsoft 365, whose CalDAV or equivalent needs OAuth |
| Mail (IMAP, read-only) | Gmail (`imap.gmail.com`, an app password, 2-step verification on), iCloud (`imap.mail.me.com`, an app-specific password), Fastmail, most hosting providers | Outlook.com and Microsoft 365, which need OAuth |
| Weather, outdoor activities, travel time | no account: Open-Meteo and OpenStreetMap, once the home location is set | |
| GitHub pull requests | a classic token with the `repo` scope | |
| Knowledge base ([knowledge.md](knowledge.md)) | Markdown notes in a GitHub repository, by hand or curated from your coding sessions by [claude-code-kit](https://github.com/robinalexandre/claude-code-kit) | |

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
| A tool you expected is missing from `tools:` | Its settings: each tool is only on when configured. |
| The model answers but never uses a tool | The model doesn't support tool calling, or is too small: try another. |
| `The model server answered 404` | `LLM_MODEL` isn't a model of that provider, or `LLM_BASE_URL` lacks its `/v1`. |
| The Tailscale address has no certificate | Enable HTTPS certificates in the tailnet's DNS settings, then restart the sidecar. |

Update: `git pull && docker compose up -d --build`.

Back up `.env`: it holds every secret and every choice. The agent itself keeps nothing on
disk; the Compose volumes only hold what can be recreated (certificates, the Tailscale
node's identity, downloaded models).

What leaves your server, and to whom: [deployment.md](deployment.md#data-that-leaves-the-server).
