# Deployment

For a first install, follow [setup.md](setup.md): it covers the same ground one choice at
a time, with ready-made Compose files. This page is the reference behind it.

## Where to run it

Any machine with Docker or Python 3.12+ that can reach Home Assistant (if you use it) and
the internet. Common setups:

| Setup | Reaching Home Assistant | Reaching the agent from outside |
|---|---|---|
| Home server or mini PC on the same network as HA | `HA_URL=http://homeassistant.local:8123` | A reverse proxy with HTTPS, or a VPN on the phone |
| A VPS | Through a private network (Tailscale, WireGuard...): nothing at home is exposed | A reverse proxy with HTTPS on the VPS |
| The HA machine itself, if it has the memory | `HA_URL=http://localhost:8123` | Same as the home server |

The agent itself is light (one Python process); the language model runs at the provider.
A small single-board computer that already runs HA may be too tight for both.

## 1. Configure

```bash
git clone <this repository> intelligent-home-agent && cd intelligent-home-agent
python3 scripts/setup.py          # or: cp .env.example .env and edit it
chmod 600 .env
```

Home Assistant token: Profile (bottom left) > Security > **Long-lived access tokens** >
Create. Ideally create a dedicated non-admin HA user for the agent and make the token
from that user.

## 2. Run

```bash
docker compose up -d --build
docker compose logs -f agent      # the startup log lists the tools that are on
```

The compose file publishes the agent on `127.0.0.1:8000` only. To expose it, add one of
the files in `compose/` (Caddy, Traefik, Tailscale, Cloudflare Tunnel: see
[setup.md](setup.md#4-reaching-the-agent)), or point a reverse proxy you already run to
that port. With a Caddy installed on the host, for example:

```
agent.example.com {
    reverse_proxy 127.0.0.1:8000
}
```
The agent starts without Home Assistant: general
questions work, home tools are off until `HA_TOKEN` is set.

Smoke test:

```bash
curl -s https://agent.example.com/health
curl -s https://agent.example.com/health/ha -H "Authorization: Bearer $AGENT_API_TOKEN"
```

Update: `git pull && docker compose up -d --build`.

## Reaching Home Assistant from a VPS

Join the VPS and the HA machine to the same private network. With Tailscale, for example:
install the Tailscale add-on in Home Assistant, put the VPS on the tailnet, and use HA's
tailnet address in `HA_URL`. Restrict the VPS node with an ACL so it can only reach HA on
port 8123.

If you'd rather not install anything on the VPS host, run the VPN as a sidecar container
in userspace mode and let the agent go through its outbound HTTP proxy, which is what
`HA_PROXY_URL` is for. Only the Home Assistant client uses that proxy.
`compose/tailscale.yml` does it (and serves the agent on the tailnet); the part that
matters for Home Assistant is:

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

- The conversation goes to the language model provider.
- Weather and outdoor forecasts: coordinates of home and of your spots go to Open-Meteo.
- Travel time: the destination goes to OpenStreetMap's Nominatim and OSRM. Once a request
  has read private data, only a place taken from the calendar in that request is accepted.
- Web search: only the query, in a separate model request.
- GitHub: fixed search queries, and the reminders folder of the repo you configured.
- ntfy: the briefing text, if you enable push.
- Connectors: what the agent asks each service you listed in `MCP_SERVERS`.
- Web search with a search engine: the query goes to the engine you chose, and the results
  to your model.
- Web pages: the address of each page opened goes to its site, from the server's own
  address. After private content, only public-service sites ([web.md](web.md)).
- Investigations, when enabled: a domain name goes to rdap.org, web.archive.org, crt.sh
  and DNS; a place or a photo's GPS position to Nominatim; a photo you sent, stripped of
  its metadata, is served at `PUBLIC_URL` for 15 minutes when a reverse image search is
  asked for.

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
