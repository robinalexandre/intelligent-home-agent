# intelligent-home-agent

A self-hosted personal assistant for your home and your day. A language model of your
choice is the brain; Home Assistant, your mailbox, your calendar, your files and the web
are what it can reach; Siri, a chat in WhatsApp or Telegram, a voice satellite or a push
notification is how you talk to it. It runs on a machine of yours, and the rules that keep
it safe are enforced by code, not by the prompt.

```
 you                                    your server                          what it reaches
┌───────────────────────────┐         ┌──────────────────────────┐         ┌─────────────────────────────┐
│ Siri · HA Assist · Alexa  │──HTTPS─►│                          │────────►│ a model: Claude, OpenAI,    │
│ Google Home · your client │         │  intelligent-home-agent  │         │ Mistral, Gemini, Ollama...  │
├───────────────────────────┤         │                          │         ├─────────────────────────────┤
│ WhatsApp · Telegram       │◄─polls──│  agent loop + tools      │────────►│ Home Assistant, devices     │
├───────────────────────────┤         │  rules enforced in code  │         │ mail, calendar, drives      │
│ push (ntfy)               │◄────────│  memory, tasks, journal  │         │ the web, your services      │
└───────────────────────────┘         └──────────────────────────┘         └─────────────────────────────┘
```

Plain commands ("turn off the living room") are best left to your voice assistant's own
Home Assistant integration: instant and local. This agent handles what needs reasoning:
multi-step requests, questions about the house and your day, a morning briefing, things to
look up, read, file or prepare, and what you would like to be told without asking.

## Quick start

With Docker, ten minutes:

```bash
git clone https://github.com/robinalexandre/intelligent-home-agent.git
cd intelligent-home-agent
python3 scripts/setup.py              # a few questions, writes .env
docker compose up -d --build
curl -s localhost:8000/health
```

The wizard asks for the essentials (a name, a model, where you live, Home Assistant or
not, how you reach it, how you talk to it), then offers the optional features in one menu
you can leave empty. Run it again later to add one.

**[Setup, step by step](docs/setup.md)** walks through every choice with its alternatives:
where it runs (a home server, a Raspberry Pi, a VPS, with or without Docker), which model
(Claude with an API key or on your Claude subscription, OpenAI, Mistral, Gemini, a local
model with Ollama...), how it reaches Home Assistant, how you reach it (home network,
Tailscale, Cloudflare Tunnel, your own domain) and how you talk to it. Then one line per
feature you can add.

## What it does

Everything is off until you configure it, and the assistant only knows about what is on.

**The basics**, with nothing but a model:

- Answers questions, with a few minutes of memory per conversation so follow-ups work, and
  hours in a chat. Replies are written to be spoken, shown as a notification or read in a
  chat. English by default, French supported.
- Reads files you send (PDF, photo, text, office documents).
- Weather at home or anywhere (Open-Meteo, no key), web search, public web pages.
- A morning briefing and an evening recap, from the sources you set up.
- Remembers what you tell it to remember, in a Markdown file you can edit.

**Talking to it** ([setup, step 5](docs/setup.md#5-talking-to-it)):

- Siri Shortcuts ([docs/siri-shortcut.md](docs/siri-shortcut.md)), Home Assistant Assist,
  Google Home and Alexa routines, ntfy push, a scheduler, your own client over HTTP
  ([docs/integrations.md](docs/integrations.md)).
- A chat in WhatsApp (through a WAHA gateway you run) or Telegram (a bot of your own):
  write, send files and voice messages, approve what it proposes with a tap
  ([docs/chat.md](docs/chat.md)).

**Home devices**

- Home Assistant: find devices, read states, call services.
- Without Home Assistant: Philips Hue, a Devialet speaker, a Google TV / Android TV
  device, an Apple TV and HomePods, a Nuki lock, Netatmo cameras and sensors, a PetKit
  feeder and litter box, a connected car (an Audi so far, through the myAudi app's
  unofficial API); what is still on or unlocked when you leave home
  ([docs/home-devices.md](docs/home-devices.md)).
- Weigh-ins from a Withings scale, said only when you ask ([docs/health.md](docs/health.md)).

**Mail and calendar** ([docs/mail-calendar.md](docs/mail-calendar.md))

- Mailboxes (IMAP), calendars (CalDAV), contacts' birthdays (CardDAV), with any provider
  that offers them: read-only by default; when you allow it, email drafts, flags, filing,
  and new events. It never sends or deletes an email.
- When to leave for an appointment (OpenStreetMap), parcels noted from shipping emails.

**Files and documents**

- File drives through rclone: Google Drive, iCloud Drive, OneDrive, Dropbox, a NAS, S3...
  browsed, searched by name and by content, read and filed, never overwritten
  ([docs/drives.md](docs/drives.md)).
- PDFs written, forms filled, pages extracted, diagrams drawn; with a key of your own,
  pictures from an image model and calculations in a sandbox
  ([docs/documents.md](docs/documents.md)).

**The web** ([docs/web.md](docs/web.md))

- Search with any model, public pages and documents read from site to site,
  investigations of a photo, a link or a site from public registries.

**Memory and knowledge**

- Facts, procedures that worked, alerts you turned down, a searchable journal of past
  conversations ([docs/memory.md](docs/memory.md)).
- Your own Markdown notes, searched before it says it doesn't know
  ([docs/knowledge.md](docs/knowledge.md)).

**Tasks and alerts** ([docs/tasks.md](docs/tasks.md))

- Tasks it runs by itself: a reminder at a time, a check every week, the briefing each
  morning; silent when there is nothing new.
- GitHub: pull requests waiting for you, your repositories read, notes left for your
  coding sessions.

**Connectors and hosting**

- Any service with an MCP server (a bank, an accounting tool, a cloud provider), read
  freely and changed only on your yes ([docs/connectors.md](docs/connectors.md)).
- Your servers at Hostinger or Hetzner Cloud: state, metrics, logs; a restart or a
  snapshot on your yes ([docs/hosting.md](docs/hosting.md)).

**Apple apps** ([docs/apple.md](docs/apple.md))

- Apple Reminders read and written, Notes read, devices found with Find My.

**Remote browser** ([docs/browser.md](docs/browser.md))

- Sites behind your login, in a real Chrome next to the agent: you log in yourself from a
  link, it then opens pages, clicks and downloads (a statement, an invoice).

**Coding jobs and nightly review**, for people who code

- A fix or a feature on one of your repositories, done by Claude Code, ending in a pull
  request, and every Claude Code session of that container followed and answered
  through the agent ([docs/jobs.md](docs/jobs.md)); a nightly review of the agent's own
  requests
  ([docs/review.md](docs/review.md)).

**Outdoor** ([docs/outdoor.md](docs/outdoor.md))

- Wind per spot, surf per beach, a dry window for a ride, a run or a hike; the wind
  measured now; a message when a good window is coming.

## Security model

The agent reads text written by other people (emails, web pages, documents) and can act
(lights, locks, drafts, files). The rules that keep one from driving the other are in the
code (`tools/base.py`), not only in the prompt, and hold whatever the model is told.

- **Authentication.** Every endpoint needs the bearer token, compared in constant time,
  except `/health`, a picture's short-lived link (below) and the coder's permission
  endpoint, which takes the coder's own token. The API's description (`/docs`, `/redoc`,
  `/openapi.json`) is not served unless `API_DOCS=true`.
- **What the agent sees of the home.** Only Home Assistant entities in
  `HA_EXPOSED_DOMAINS`, never a denylisted one. A service can only target entities of its
  own domain, and targets can't be smuggled in through `data`.
- **Taints.** A request is *tainted* once it has read content written by others: private
  content (an email, a calendar event, a file from a drive, an account, a note), a web
  page, a web search answer, or a file you sent. From the next batch of tool calls on,
  every tool that acts or writes is held back for the rest of that request, so injected
  text can't trigger an action.
- **Your go, on exactly what you were shown.** A held-back action is not lost: it is
  parked with its exact arguments, and the reply ends with a numbered list written by the
  code, not by the model:

  ```
  Waiting for your go ("yes", or "yes except 2"):
  1. Search the web for "bakery opening hours" and carry on
  2. My Bank: add_customer {"name": "Acme"}
  ```

  It runs only when your next message agrees, in a request that has read nothing else.
  Nothing read in between can change what it does; what was never listed can't run; it is
  forgotten with the conversation. In a chat the list is a choice you tick.
- **Some actions always wait**, even in a clean request: a connector's write, a
  deployment, opening a door without proof, turning a camera's monitoring off, a plan of
  several steps. Home Assistant actions on `HA_SENSITIVE_DOMAINS` (locks, alarms, covers)
  need your yes in a *later* message: the model can't confirm for itself.
- **What destroys or runs later.** What deletes only happens on your own message. A task
  is run later with nobody watching, so one is only scheduled in a request that read
  nothing written by others (else it waits for your go, shown word for word). A
  repeating task starts as a request that read others' content: it reads and reports,
  and neither searches the web nor writes unless it runs *fresh*. What a task would do
  waits for your go in the chat, or is left out when its report goes by push.
- **What leaves the house.** A web search query, an address opened, a prompt for an image
  model are text that goes out. After private content was read, a search waits for your
  go, and only your country's public-service sites and `WEB_TRUSTED_DOMAINS` still open.
  In any request, a query that repeats a number, an email address, a long token or a run
  of words from what the request holds is refused. Only public addresses are ever opened
  (never the home network, a container or cloud metadata, checked after every redirect).
- **What is kept.** After a tainted request, only the question, the reply and the names
  of the tools used stay in the conversation; the private content is dropped, so it can't
  steer later requests. The journal keeps neither the reply nor the tool arguments of
  such a request. A request that fails is rolled back entirely.
- **Memory.** A remembered fact comes back in every later request, so nothing is
  remembered, forgotten or saved as a procedure after a read without your go, shown with
  every word; passwords, keys and tokens are refused. Facts are given to the model as
  data, not instructions.
- **Secrets** stay in `.env` (mode 600 when written by the wizard, which never prints
  them). Files the agent keeps are readable by its user only. The Siri Shortcut files hold
  the token: don't share them.

Each feature adds its own rules on top, described on its page:

| Feature | In short |
|---|---|
| [Mail and calendar](docs/mail-calendar.md#what-the-agent-does) | Folders are opened read-only and nothing is marked read. Writing is off until you turn it on; even then no email is ever sent or deleted. |
| [Home devices](docs/home-devices.md#safety) | A door opens only with proof only you have (an authenticator code, letters of a secret phrase) or your yes. What a lock's log, a camera, a scale report is your data: after reading it a search waits. |
| [File drives](docs/drives.md#what-the-agent-does) | Reading a file taints like an email. No file is ever replaced; a deletion needs your own message and keeps a copy first. A drive can be read-only. |
| [Web pages and investigations](docs/web.md) | After web content, the agent follows what pages put forward and plain addresses; a page that hides words for an assistant ends free navigation. A photo goes online for a reverse image search only if you sent it, without its metadata, for 15 minutes at an unguessable address. Never to identify a private person. |
| [Documents](docs/documents.md#what-it-may-and-may-not-do) | PDFs and drawings are rendered from an allowlist that can load nothing. Code runs in the provider's sandbox, never on your machine. |
| [Chat channels](docs/chat.md) | Only you reach the agent: one WhatsApp chat, or the Telegram private chats you listed. A forwarded or quoted message, a file, a group's messages are others' words. |
| [Connectors](docs/connectors.md#what-the-agent-may-do) | Reading taints; a write always waits, shown with its exact arguments; with `writes: off` the write tools don't exist. |
| [Hosting](docs/hosting.md#what-the-agent-may-and-may-not-do) | A fixed list of API paths. A restart needs your own clear request; a deployment always waits, shown whole. No deleting. |
| [Apple apps](docs/apple.md) | The agent keeps a session, never the password. After a Find My read no new site opens and a search waits, so a position can't leave. |
| [Remote browser](docs/browser.md#guards) | You type your passwords yourself, in a page only you reach. Clicks only run in a request that read nothing but that same site. Chrome can't reach private addresses. |
| [Tasks and notes](docs/tasks.md#what-the-assistant-may-do-with-tasks) | Scheduled only in a clean request or on your go; only your own message changes or deletes one. |
| [Coding jobs](docs/jobs.md#guardrails), [nightly review](docs/review.md) | The coder has none of the agent's secrets and can touch only what its GitHub token can. What its sessions say is data, and only your own message is passed to one. The review hands a day of your conversations to the coder, and only to a private repository. |
| [Claude subscription](docs/subscription.md#what-claude-code-may-do-here) | Claude Code is used as the model only: its own tools are off, the agent's tools and rules apply. |

What leaves your server, and to whom: [docs/deployment.md](docs/deployment.md#data-that-leaves-the-server).

## Configuration

Environment variables, usually in `.env`. The wizard writes the common ones,
[.env.example](.env.example) lists all of them with a comment, and
**[docs/settings.md](docs/settings.md)** is the full reference. The essentials:

| Setting | Default | What it does |
|---|---|---|
| `AGENT_API_TOKEN` | required | Bearer token every client sends. |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, `mistral`, `gemini`, `groq`, `openrouter`, `ollama`, `lmstudio`, `openai-compatible`. |
| `LLM_API_KEY` | required, except local servers and a subscription | The provider's key. |
| `LLM_MODEL` | `claude-sonnet-5-5` with Anthropic, else required | It must support tool calling. |
| `LLM_BASE_URL` | the provider's | An OpenAI-compatible server's address. |
| `ENGINE`, `CLAUDE_CODE_OAUTH_TOKEN` | `api`, unset | `subscription`: Claude answers on your Claude plan ([docs/subscription.md](docs/subscription.md)). |
| `ASSISTANT_NAME`, `USER_NAME` | `Assistant`, unset | What it calls itself, and you. |
| `LANGUAGE`, `TIMEZONE` | `en`, `UTC` | `en` or `fr`; an IANA time zone. |
| `HOME_LATITUDE`, `HOME_LONGITUDE`, `HOME_CITY`, `HOME_COUNTRY` | unset | Home: weather, travel time, local results. |
| `HA_URL`, `HA_TOKEN` | local URL, unset | Home Assistant; no token means no home tools. |
| `COMPOSE_FILE`, `EXTRAS` | the agent alone, `documents` | Docker: the files of `compose/` in use, and the optional parts built into the image. |
| `NTFY_URL` | unset | Push channel. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` | unset | A Telegram chat. |
| `WAHA_API_KEY`, `WHATSAPP_CHAT_ID` | unset | A WhatsApp chat (`compose/waha.yml`). |
| `DATA_DIR` | `data` | Where the agent keeps what survives a restart (the `agent_data` volume in Docker). |

### Model providers

The agent loop and the tools speak one message format and call the model through a small
interface (`LLMClient` in `llm.py`). Two clients are shipped: **Anthropic**, and
**OpenAI-compatible** for every server that speaks the Chat Completions API (OpenAI,
Mistral, Gemini, Groq, OpenRouter, Ollama, LM Studio, llama.cpp, vLLM, a gateway). With
`ENGINE=subscription`, Claude Code answers on your Claude plan through the same tools and
rules. A provider with another API is one class ([AGENTS.md](AGENTS.md)).

A few features need something only some providers have, and say so: web search uses
Anthropic's own tool with Claude and a search engine of your choice with the others; the
code sandbox and reading scanned PDFs of a drive need Anthropic's API; coding jobs are run
by Claude whatever model answers you.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python scripts/setup.py               # or: cp .env.example .env and edit it
uvicorn intelligent_home_agent.main:app --reload

curl -s localhost:8000/v1/ask \
  -H "Authorization: Bearer $AGENT_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Which lights are on?"}'
```

Tests and lint: `pytest -q && ruff check . && ruff format --check .`. How the code is
laid out, the tool flags and how to add a feature, a provider or a chat channel:
[AGENTS.md](AGENTS.md).

```
src/intelligent_home_agent/   the app, the agent loop, prompts, settings, tools/
scripts/                      setup.py (the wizard), build_shortcuts.py (Siri), mcp_login.py,
                              one login or pairing script per device that needs it
compose/                      one Compose file per optional container or way to reach the agent
coder/, agents/, environments/  the coding jobs' container, house rules and sandbox
docs/                         setup step by step, one page per feature, settings reference
tests/                        pytest, no network
```

## Not verified yet

The test suite covers the code without the network, against simulated services. What
was **not run against the real service** yet, collected from the feature pages:

- **Telegram**: written from the Bot API's documentation, tested against a fake only.
- **WhatsApp**: this version ran only against a fake WAHA; `compose/waha.yml` was not started.
- **Remote browser**: none of its Compose files (`browser*.yml`, `vpn.yml`, the Caddy and
  tailnet gates) was started; they were checked as valid YAML only.
- **Hetzner Cloud**: written from its documentation. Hostinger was used for real.
- **File drives**: only iCloud Drive was run for real; every other service, several
  drives at once and copies between drives ran against a simulated rclone.
- **Mail and calendar**: written against iCloud and Gmail; Fastmail, Nextcloud and other
  servers were not tried live.
- **Apple Reminders, Notes, Find My**: not tried against a real iCloud account from this
  repository.
- **Measured wind**: the airport-report (METAR) reader is new and has not met the real
  service.
- **The TV navigator**: used on one Google TV projector with Claude; other models were
  not tried on a real screen.
- **The car**: run on one Audi and one account; nothing was run against other models or
  markets, and no other make exists yet.
- **Not recorded**: runs against real services of the model providers other than
  Anthropic, of the search engines (DuckDuckGo, SearXNG, Brave, Tavily), of an
  OpenAI-compatible image server, of the Home Assistant, Google Home and Alexa recipes,
  and a start from this repository of `compose/kb.yml`, `compose/drive.yml` and
  `compose/coder.yml`.

If you run one of these, an issue saying what worked and what didn't is welcome.

## Roadmap

- [ ] Calendars and mailboxes that need OAuth (Google Calendar, Microsoft 365, Outlook.com)
- [ ] Connectors for MCP servers that run as a local command
- [ ] Optional: Home Assistant's MCP server instead of the REST tools
- [ ] Package as a Home Assistant add-on
- [ ] Rate limiting

## License

MIT, see [LICENSE](LICENSE).
