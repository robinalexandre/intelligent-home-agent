# Remote browser: sites behind your login

Optional, Docker only. A real Chrome runs next to the agent. You log into a site in it from
a link (your password never goes through the agent), the session stays in Chrome's profile,
and the agent can then open pages, click and download on that site: fetch a certificate from
a tax office or an insurer, a statement from a bank, an invoice from a shop account, and
keep the file or attach it to an email draft. It also opens public pages that need
JavaScript, which the plain page reader ([web.md](web.md)) shows empty.

```
you: the link the agent sent  ─►  HTTPS (Caddy, Traefik, Tailscale)  ─►  browser-gate ─► Chrome's page
                                                                          └─ asks the login page first
agent ──control port, internal network only──►  browser-cdp relay ──►  the same Chrome
Chrome ──►  browser-proxy (refuses private addresses)  ──►  websites
```

**None of the Compose files of this page could be started while they were written**: no
Docker was available. The agent's code and its tests are the ones of an installation that
runs daily behind Traefik on the host network; the files here were reshaped for this
repository's `compose/` overlays (a gate container, Caddy, a tailnet-only variant, a VPN
provider as a setting) and only checked as valid YAML. Expect to adjust something on first
start, and see "If it doesn't start" at the end.

## What the agent does with it

- `browser_open` opens a page and returns its text and numbered elements (links, buttons,
  fields), or downloads it if it is a document. Chrome's PDF viewer is turned off by a
  managed policy, so a PDF is always saved as a file rather than shown.
- When a page asks to log in, `request_login` opens it in Chrome and returns the link the
  agent sends you. You log in there, say it's done, and the agent carries on. Each site is
  asked once: the session lasts as long as the site keeps it (months for some, hours for
  banks and administrations).
- `browser_click` (with `download` to keep the file) and `browser_type` act inside your
  accounts. Each comes back to the model with a capture of the screen, to check what
  happened.
- `browser_screenshot` keeps a PNG of the page the request opened (a product and its
  price, a confirmation), which can then be attached to a draft.

## Guards

- **Your passwords stay yours.** The agent never types into a password field and is told
  never to ask for one. You log in yourself, in the browser's page.
- **Acting needs a clean request.** On any site, clicking and typing work as long as the
  request read nothing but pages of that same site (a site you logged into groups its
  subdomains). After reading an email, a file, a search answer or another site, they are
  refused: the agent says what it would do and acts on your next message, or proposes the
  whole job as a plan you approve once. A malicious email can't steer it inside your
  accounts. A hostile page can only steer clicks on its own site, in a request you started
  about that site; a page whose text speaks to an assistant ends navigation and actions
  for the request.
- **Where it may go once something was read.** A URL can carry data out, so after private
  content (mail, files, an account) the browser only opens your country's public-service
  sites, `WEB_TRUSTED_DOMAINS`, and the sites you logged into. After public pages only,
  it also follows the links those pages listed and plain addresses, like the page reader.
  This applies to every top-level navigation Chrome makes, redirects included.
- **The list of your sites** widens that, so a site joins it only on your own request: you
  ask for the login, you approve a plan that names the site, or you ask for the site again
  after its login link. A site you logged into by yourself in the browser's page joins when
  a request of yours opens it and Chrome already holds a session there.
- **No way inside your networks.** Chrome sits on internal Docker networks. Its only way
  out is `browser-proxy` (Squid, `compose/browser/squid.conf`), which resolves every name
  itself and refuses private destinations, so neither a redirect, a page's own requests
  nor DNS rebinding reach this machine's services or your home. The agent checks the same
  on its side for clear errors.
- **The control port** listens on Chrome's localhost; `browser-cdp` relays it on an
  internal network to the agent's address alone.
- **The page you log in through** is the sensitive part: whoever reaches it uses Chrome
  with your sessions. It is never published as such; see the next section.
- The profile (`browser_profile` volume) holds your sessions: treat the machine like your
  own computer. To log out of a site, or of everything, open the page and use Chrome
  itself.

## Reaching the browser's page: two ways

| | Behind a login page | Tailscale only |
|---|---|---|
| Who can open it | anyone who passes the login, from anywhere | devices signed in to your tailnet |
| Login | Authelia: a passkey alone, or password + one-time code | none of its own: the tailnet is the login |
| You need | your own domain (two more names), Caddy or Traefik | `compose/tailscale.yml`, the Tailscale app on the phone |
| Files | `browser.yml`, `browser-login.yml`, `browser-caddy.yml` or `browser-traefik.yml` | `browser.yml` |

The second is simpler and exposes nothing to the internet. Take the first when you want to
log in from a device that is not on your tailnet.

`python scripts/setup.py` asks for either when you choose Caddy, Traefik or Tailscale as
the way to reach the agent, and writes the lines below.

### With Caddy or Traefik, behind a login page

1. DNS: two more names pointing to this machine, under one parent domain, e.g.
   `browser.example.com` and `auth.example.com`.
2. In `.env` (Traefik: replace `caddy` twice):
   ```bash
   COMPOSE_PATH_SEPARATOR=:
   COMPOSE_FILE=docker-compose.yml:compose/caddy.yml:compose/browser.yml:compose/browser-login.yml:compose/browser-caddy.yml
   EXTRAS=documents,browser                  # add "browser" to what EXTRAS already holds
   BROWSER_PUBLIC_URL=https://browser.example.com
   BROWSER_DOMAIN=browser.example.com
   AUTH_DOMAIN=auth.example.com
   AUTH_COOKIE_DOMAIN=example.com            # the parent both names are under
   AUTH_SESSION_SECRET=<openssl rand -hex 32>
   AUTH_STORAGE_KEY=<openssl rand -hex 32>   # encrypts the passkeys and one-time-code secrets
   AUTH_JWT_SECRET=<openssl rand -hex 32>
   ```
3. Set the password: `scripts/browser-auth.sh user` (asked there, never echoed, at least
   12 characters; stored as an argon2 hash in the `browser_auth` volume).
4. `docker compose up -d --build`. Chrome takes about 600 MB of memory, the login page
   about 30 MB.
5. Open `https://browser.example.com`, log in with the password, and add in the login
   page's settings an authenticator app (one-time codes) and a passkey on each device.
   Each asks for a one-time code that would normally come by email: there is no mail here,
   `scripts/browser-auth.sh code` shows it. A passkey that checks a fingerprint or a face
   then logs in alone.

How it holds together: `browser-gate` (a small Caddy, `compose/browser/gate-login.Caddyfile`)
is the only door to Chrome's page and shows every request to Authelia first; without
Authelia running it serves nothing. The session lasts four weeks (one without a visit), and
a few wrong tries lock the login for a while. The cookie is set on `AUTH_COOKIE_DOMAIN`, so
other sites you serve under that domain receive it too (HttpOnly, Secure). With Caddy,
`compose/browser/Caddyfile` replaces the one-line command of `compose/caddy.yml`.

### With Tailscale only

1. In `.env`:
   ```bash
   COMPOSE_PATH_SEPARATOR=:
   COMPOSE_FILE=docker-compose.yml:compose/tailscale.yml:compose/browser.yml
   EXTRAS=documents,browser
   BROWSER_GATE=tailnet
   TS_SERVE=serve-browser
   BROWSER_PUBLIC_URL=https://home-agent.<your tailnet>.ts.net:8443
   ```
2. `docker compose up -d --build`, then open that address from a device on your tailnet.

`compose/tailscale-serve-browser.json` serves the agent on port 443 and the browser's page
on 8443, to the tailnet only. Don't combine `BROWSER_GATE=tailnet` with Funnel or any
public address: that gate asks nothing. If you share your tailnet with other people, limit
who reaches this node in the tailnet's access rules, or use the login page instead.

### Cloudflare Tunnel, another proxy, no Docker

- **Cloudflare Tunnel**: use `browser.yml` and `browser-login.yml`, and give the tunnel two
  more public hostnames: the browser's name to `http://browser-gate:8080`, the login's to
  `http://browser-auth:9091`. (Cloudflare Access in front of `browser-gate` with
  `BROWSER_GATE=tailnet` would be another login; not tried.)
- **Another reverse proxy** (nginx, a NAS): the same two routes; publish the two ports to
  the host's loopback yourself. It must pass `Host` and `X-Forwarded-Proto` on.
- **Tailscale Funnel**: not offered; Authelia needs two names under one domain.
- **Without Docker**: not supported; the isolation above is made of Docker networks.

## Seen from another country (optional)

Flights, hotels, car rentals, subscriptions and some shops price by where the visitor is.
With a VPN subscription, `browser_open` takes a `country` (its English name, e.g. `Spain`)
and shows the page as seen from there:

```
agent ──► Chrome, a window of its own ──► browser-proxy :3129 ──► vpn (gluetun) ──► site
```

- The window has none of your sessions or cookies, asks pages in the country's language
  and keeps its time zone, so your accounts never see a login from abroad. Downloads only
  work in the normal window.
- The VPN runs only while a request browses abroad: the agent picks the country through
  gluetun's control server, waits until the internet sees that country, and stops the
  tunnel when the request ends, even when it fails. A window left unused for 10 minutes
  closes too, and a watch stops the tunnel every minute when nothing browses abroad. One
  request at a time browses abroad.
- The abroad port of `browser-proxy` makes the same checks as the normal one, then hands
  the page to gluetun only, never directly.
- The agent never pays: it gives the price and says whether a card and an address from
  your country can buy there.

[gluetun](https://github.com/qdm12/gluetun) speaks to most VPN providers, so the provider
is a setting. Add `compose/vpn.yml` at the end of `COMPOSE_FILE` (after `browser.yml`) and
in `.env`:

```bash
VPN_SERVICE_PROVIDER=mullvad        # gluetun's name for your provider
VPN_TYPE=wireguard                  # or openvpn
VPN_CONTROL_KEY=<openssl rand -hex 32>
# then what your provider uses, for example:
WIREGUARD_PRIVATE_KEY=...           # WireGuard (Mullvad, NordVPN, Surfshark, IVPN, AirVPN...)
WIREGUARD_ADDRESSES=...             # some providers give one with the key (Mullvad, IVPN...)
# OPENVPN_USER=... and OPENVPN_PASSWORD=...     # OpenVPN (ExpressVPN, PIA, ProtonVPN...)
```

gluetun's wiki has one page per provider saying which of these it needs and where to find
them in your account; a setting it needs that `compose/vpn.yml` doesn't pass can be added
to that file's `environment`. Country names are gluetun's for that provider (`Spain`,
`United States`); when the provider doesn't serve one, the agent says so. Only a provider
whose servers gluetun can choose by country works here, not a single fixed server
(`custom`).

## If it doesn't start

- `docker compose config` first: it shows the merged file. The agent must be on the
  `browser` network at `10.89.42.20` and still on `default`. If your Compose version
  refuses to merge the agent's `networks` between files, write them out in one overlay.
- "Pool overlaps": the range `10.89.42.0/24` is taken on your machine. Change it in
  `compose/browser.yml`, `compose/vpn.yml` and the two `compose/browser/squid*.conf`.
- The agent says "The remote browser is not running": `docker compose logs browser
  browser-cdp`. The Chrome image and playwright are pinned together (`pyproject.toml`
  says why); a newer image may need a newer playwright.
- The login page loops or answers 401/403: the gate must see the original `Host` and
  `X-Forwarded-Proto: https` (`docker compose logs browser-auth browser-gate`), and the
  three names must fit `AUTH_COOKIE_DOMAIN`.
- Pages don't load in Chrome: `docker compose logs browser-proxy`; Squid must have
  started with the mounted configuration.
- No tools at all: the agent logs why at start (`BROWSER_PUBLIC_URL` empty, or the
  `browser` extra missing from the image: rebuild after changing `EXTRAS`).
