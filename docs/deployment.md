# Deployment

For a first install, follow [setup.md](setup.md): it covers the same ground one choice at
a time. This page is the reference behind it: running without Docker, a reverse proxy of
your own, reaching Home Assistant from a VPS, and what leaves the server.

## Where to run it

Any machine with Docker or Python 3.12+ that can reach Home Assistant (if you use it) and
the internet. Common setups:

| Setup | Reaching Home Assistant | Reaching the agent from outside |
|---|---|---|
| Home server or mini PC on the same network as HA | `HA_URL=http://homeassistant.local:8123` | Tailscale, a tunnel, or a reverse proxy with HTTPS |
| A VPS | Through a private network (Tailscale, WireGuard...): nothing at home is exposed | A reverse proxy with HTTPS on the VPS |
| The HA machine itself, if it has the memory | `HA_URL=http://host.docker.internal:8123` (`http://localhost:8123` without Docker) | Same as the home server |

The agent itself is light (one Python process); the language model runs at the provider.
A small single-board computer that already runs HA may be too tight for both. Optional
parts weigh more: the remote browser some 700 MB of memory, the speech model and the
knowledge base's embedding model about 500 MB of disk each, Claude Code 400 MB.

## With Docker

```bash
git clone https://github.com/robinalexandre/intelligent-home-agent.git && cd intelligent-home-agent
python3 scripts/setup.py          # or: cp .env.example .env && chmod 600 .env, and edit it
docker compose up -d --build
docker compose logs -f agent      # the startup log lists the tools that are on
```

`docker-compose.yml` publishes the agent on `127.0.0.1:8000` only. To expose it, add one of
the files in `compose/` (Caddy, Traefik, Tailscale, Cloudflare Tunnel: see
[setup.md](setup.md#4-reaching-the-agent)), or point a reverse proxy you already run to
that port. With a Caddy installed on the host, for example:

```
agent.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

The agent starts without Home Assistant: general questions work, home tools are off until
`HA_TOKEN` is set.

Smoke test:

```bash
curl -s https://agent.example.com/health
curl -s https://agent.example.com/health/ha -H "Authorization: Bearer $AGENT_API_TOKEN"
```

Update: `git pull && docker compose up -d --build`. What to back up:
[setup.md, step 6](setup.md#6-check-update-back-up).

## Without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install ".[documents]"             # add the extras you use: ".[documents,voice,icloud]"
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

The agent keeps its files in `DATA_DIR` (`data/` in the working directory). Reaching it is
yours to set up (a reverse proxy, `tailscale serve 8000`, `cloudflared`): the files in
`compose/` show what each one needs. The parts that are containers of their own are yours
to run too, with the agent told where they are: a WAHA gateway (`WAHA_URL`), the knowledge
base (`KB_URL`), rclone (`DRIVE_URL`), the coder (`CODER_URL`). The remote browser is not
supported without Docker.

## Reaching Home Assistant from a VPS

Join the VPS and the HA machine to the same private network. With Tailscale, for example:
install the Tailscale add-on in Home Assistant, put the VPS on the tailnet, and use HA's
tailnet address in `HA_URL`. Restrict the VPS node with an ACL so it can only reach HA on
port 8123.

If you'd rather not install anything on the VPS host, run the VPN as a sidecar container
in userspace mode and let the agent go through its outbound HTTP proxy, which is what
`HA_PROXY_URL` is for (and `HOME_PROXY_URL`, for devices driven without Home Assistant:
[home-devices.md](home-devices.md#reaching-the-home-network)). `compose/tailscale.yml`
does it (and serves the agent on the tailnet); the part that matters for Home Assistant is:

```yaml
services:
  agent:
    environment:
      HA_PROXY_URL: http://tailscale:1055
    depends_on: [tailscale]
  tailscale:
    image: tailscale/tailscale:stable
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY:?set TS_AUTHKEY}
      TS_USERSPACE: "true"
      TS_STATE_DIR: /var/lib/tailscale
      TS_OUTBOUND_HTTP_PROXY_LISTEN: 0.0.0.0:1055
    volumes: [tailscale_state:/var/lib/tailscale]
    restart: unless-stopped
volumes:
  tailscale_state:
```

## Data that leaves the server

Always, once a model is set:

- The conversation, with what the tools returned, goes to the language model provider.
  With `ENGINE=subscription` it goes to Anthropic through Claude Code, on your Claude
  plan; Claude Code's telemetry is switched off, and its transcripts of the turns stay on
  the server, in `DATA_DIR/claude` ([subscription.md](subscription.md)). With a local
  model (Ollama, LM Studio) it stays on your machine.

Per feature, only when it is on:

- Weather and outdoor forecasts: coordinates of home and of your spots go to Open-Meteo;
  a spot's coordinates to the sources of measured wind you chose.
- Travel time: the destination goes to OpenStreetMap's Nominatim and OSRM. Once a request
  has read private data, only a place taken from the calendar in that request is accepted.
- Web search: only the query, in a separate model request; with a search engine
  (DuckDuckGo, SearXNG, Brave, Tavily), the query goes to that engine and its results to
  your model.
- Web pages: the address of each page opened goes to its site, from the server's own
  address. After private content, only public-service sites ([web.md](web.md)).
- Investigations: a domain name goes to rdap.org, web.archive.org, crt.sh and DNS; a place
  or a photo's GPS position to Nominatim; a photo you sent, stripped of its metadata, is
  served at `PUBLIC_URL` for 15 minutes when a reverse image search is asked for.
- Mail, calendar, contacts, file drives, Apple apps, home devices with a cloud (Nuki,
  Netatmo, PetKit), Withings, a hosting provider: the agent talks to the account you gave
  it, and what it reads there goes to the model like the rest of the conversation.
- Connectors: what the agent asks each service you listed in `MCP_SERVERS`.
- GitHub: fixed search queries for your pull requests; the notes, memory or tasks
  repository you configured; with `GITHUB_REPOSITORIES`, the files it reads and the
  dependency names it checks at OSV.dev.
- Chat channels: your messages and the agent's replies go through WhatsApp (by the WAHA
  gateway you run) or Telegram. ntfy: the text of what is pushed.
- Generated pictures: the prompt and the pictures you name, to Google or OpenAI. The code
  sandbox: the task and the files you name, to Anthropic.
- Remote browser: pages are fetched by the Chrome next to the agent, from the server's
  address or, when you ask for another country, through your VPN provider.
- Coding jobs: the request and the repository go to Claude (Claude Code on your plan, or
  Managed Agents on the API). The nightly review also hands it a day of the journal, your
  conversations: [review.md](review.md) says exactly what.

Voice messages are transcribed on the server; the knowledge base's index and the drives'
index are built on the server.

## Later: packaging as a Home Assistant add-on

An add-on is a Docker image plus a small manifest, published from a Git repository:

```
intelligent-home-agent-addon/
  repository.yaml        # name/url of the add-on repository
  intelligent_home_agent/
    config.yaml          # name, slug, arch, ports, options schema (API keys...)
    Dockerfile           # FROM the HA base image, installs this package
    run.sh               # reads /data/options.json, exports env vars, starts uvicorn
```

Add-ons can use the Supervisor token, so no long-lived token would be needed. Not done yet.
