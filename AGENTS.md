# AGENTS.md

Notes for any coding agent (and person) working in this repository.

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate   # or: uv venv && uv pip install ...
pip install -e ".[dev]"
```

## Checks (all three must pass, CI runs them)

```bash
ruff check .
ruff format --check .        # `ruff format .` to fix
pytest -q
```

Tests never touch the network: HTTP goes through `respx`, the model through `FakeLLM` in
`tests/conftest.py`. If your shell exports `GITHUB_TOKEN`, `ANTHROPIC_API_KEY` or other
settings, tests that build `Settings` clear the ones they care about; when in doubt run
`env -u GITHUB_TOKEN pytest -q`.

## Layout

```
src/intelligent_home_agent/
  main.py           FastAPI app: /v1/ask, /v1/briefing, /v1/files, auth, tool registry
                    wiring, the list of what waits for the user's go under a reply
  agent.py          tool-use loop: files, long results, taint tracking, memory scrubbing,
                    displays (voice, notification, chat), journal hook
  llm.py            provider interface (LLMClient, LLMResponse), the Anthropic client and
                    the OpenAI-compatible one (OpenAI, Mistral, Ollama...)
  engine.py         Claude on a Claude subscription (ENGINE=subscription): a turn run by
                    Claude Code through the Agent SDK, the tools still run by the registry;
                    subscription_first and SubscriptionFirst for the server's own model
                    calls; engines.py builds one engine per model (docs/subscription.md)
  prompts.py        system prompt, built from what is configured
  i18n.py           every word the code says to the user, per language
  activities.py     outdoor activity catalog (stdlib only, used by scripts/setup.py)
  config.py         settings (env / .env)
  conversations.py  conversation state: messages, files (Attachment), writes waiting for
                    the user's go (Proposal), pending Home Assistant confirmations; what
                    of a chat is saved in DATA_DIR (ConversationStore.save, FileStore)
  config_crypt.py   git-crypt as a codec, for a memory or notes kept in an encrypted
                    repository
  tasks.py          tasks the agent starts by itself: TaskRunner (a job of the app),
                    TaskReports; main.run_task and run_digest answer them (docs/tasks.md)
  deliver.py        where a report written with nobody asking goes: ReportChannel
                    (ntfy; add a chat here), Delivery, Outbox (GET /v1/tasks/reports)
  leaks.py          keeps what a request holds out of text the model writes for the outside
  journal.py        one line per request, per day, in DATA_DIR/journal
  drive_content.py  the text inside the files of the drives (tools/drives.py), indexed
                    for search; scanned PDFs read by the model where the provider can
  office.py         Word, Excel, PowerPoint, OpenDocument and iWork files as text or preview
  notify.py         ntfy push
  shared_links.py   short-lived public links to a picture (reverse image search)
  home_devices.py   devices driven without Home Assistant (Hue, Devialet, Android TV,
                    Apple TV and HomePods, Nuki, Netatmo, PetKit), each on with its
                    settings; unofficial libraries are optional extras
  departure.py      POST /v1/departure: what is still unlocked or on, fixes parked
  pet_alerts.py     the pet supplies watched and pushed through ntfy
  tools/base.py     Tool and its flags, ToolContext, ToolRegistry (the rules live here)
  tools/proposals.py     run_proposals, propose_action, propose_plan...: the user's go
  tools/long_results.py  read_result_part, for results too long for one read
  tools/memory.py        facts kept about the user, in a local file or a repository
                         (TextFile: LocalFile, GitHubFile); procedures.py, turned_down.py
                         and past_conversations.py sit beside it (docs/memory.md)
  tools/            one module per capability, each with a build_*_tools() factory
scripts/setup.py         interactive .env writer (stdlib only)
compose/                 one Compose file per way to reach the agent, picked by COMPOSE_FILE
scripts/build_shortcuts.py  Siri Shortcuts builder (macOS)
```

## Conventions

- Python 3.12+, type hints, `from __future__ import annotations`, line length 100.
- A tool is a `Tool(name, description, input_schema, handler)`; its factory returns `[]`
  when it isn't configured, so the registry and the prompt only show what works.
- Errors meant for the model are `ToolError`; anything else is logged as a crash.
- Security rules are flags on `Tool`, enforced in `ToolRegistry.run` and `Agent`; see
  "Tool flags" below. Never weaken them, the entity policy in `tools/home_assistant.py`,
  or the later-turn confirmation for sensitive domains, without a test that shows why.
- There is one mechanism for "this waits for the user's go": the registry parks the call
  and `main.with_waiting` lists it under the reply. Don't build another one in a tool.
- New settings go in `config.py`, `.env.example` (with a comment), and the configuration
  table in README.md; ask for them in `scripts/setup.py` if a new user needs them.
- A new way to deploy is a file in `compose/` that adds to `docker-compose.yml`, a choice
  in `ACCESS` in `scripts/setup.py`, and a row in docs/setup.md.
- New user-facing words go through `i18n.py` (English first, French kept up to date).
- Test data stays fictional: `0.0, 0.0` or famous public places, names like "Alex",
  `example.com` addresses.
- Docs: plain and short; say what works and what doesn't.

## Tool flags

A request is *tainted* once it read content written by others. `ToolContext.tainted` is
true after a tool with `taints` (private content: `ctx.private_read`), `reads_pages`
(`ctx.page_hosts`) or `reads_web` (`ctx.web_read`) ran, when the user sent a file, or when
the caller says so. Calls of one batch were all decided before any ran, so a taint applies
from the next batch on. After a tainted request only the question, the reply and the names
of the tools used stay in memory.

| Flag on `Tool` | Meaning |
|---|---|
| `taints` | Output is private content written by others (mail, calendar, an account). |
| `reads_pages` | Output is a web page; the handler records its site with `ctx.read_page(host)`. |
| `reads_web` | Output is written from public web pages (a search answer): others' words, nothing private. |
| `needs_clean_turn` | Acts on the home or writes: in a tainted request (or a reply quoting a message) it is parked for the user's go instead of run. |
| `never_park` | With `needs_clean_turn`: refused instead of parked (`run_proposals` itself). |
| `needs_user_request` | Destroys something: in a scheduled task it waits for the user (parked) or is refused. |
| `proposable` | May be parked even without `needs_clean_turn`; also parks in a repeating task. |
| `always_propose` | Parked even in a clean request: the user always sees it first (connector writes, plans). |
| `runs_shown_writes` | Only runs what the user was already shown, so it works in a reply quoting the list. |
| `describe` | `async (args) -> str`: the action in one line for the user, built by the code (use `i18n`). Default: the tool's name and every argument. `describe_whole(label)` shows each argument. |
| `vet` | `(args, ctx)`, sync or async: raises `ToolError` for what could never run, before it is parked. |
| `multiline_args` | Argument names that may hold line breaks (shown as ⏎, needs `describe`). |
| `clean_runs_in_tasks` | In a repeating task's run, refused with a note that later runs start clean. |

Parking: `ToolRegistry.run` stores a `Proposal` (tool, exact arguments, summary) in the
conversation and adds a `{"type": "proposal", "number", "summary"}` action; the tool
returns `{"status": "waiting_for_the_user"}` to the model. `main.with_waiting` writes the
numbered list under the reply and marks the proposals shown; only shown ones can run, and
only through `run_proposals` in a later turn that read nothing. A handler can ask for the
same by raising `ParkInstead` (a search after a private read), or park another tool's call
with `park(tool, args, ctx)`.

`ToolContext` (one per request) carries: `conversation` (with `files`, `proposals`,
`long_results`), `actions` (what the client or `main` does after the reply: `reminder`,
`proposal`, `written`, `share_file`, `question`), the taint state above, `leaks` and
`keep_in(text, "query" | "address")` for text that leaves, `offered_urls`, `named_hosts`
and `free_site()` for browsing tools, `scheduled`, `repeating`, `quoted`, `approved_plan`,
`approving`, `answerable`, `calendar_places`, and `cleanups` (awaited when the request
ends).

A handler returns JSON-able data or a string; an `Attachment` (the model reads the file);
or `WithImage(data, image)` (data plus a picture). Results over 12 000 characters are kept
apart automatically. To hand a file to the user: `ctx.conversation.keep_file(Attachment(
name, mimetype, data, origin="agent"))` then `ctx.actions.append({"type": "share_file",
"name": kept.filename})`.

## Porting or adding a feature

One capability is one module in `tools/` plus its wiring. Do every step; skip one only
when it truly doesn't apply, and say so in the pull request.

1. **`src/intelligent_home_agent/tools/<feature>.py`**: a client class if it talks to a
   service, and `build_<feature>_tools(...) -> list[Tool]` that returns `[]` when the
   feature isn't configured. Set the flags from the table above: `taints=True` on
   anything that returns text written by others, `needs_clean_turn=True` on anything that
   acts or writes (it then waits for the user's go by itself after a read),
   `needs_user_request=True` on anything that destroys, a `describe` for every write, a
   `vet` for what can be refused before asking. Errors for the model are `ToolError`.
   Take `language` as an argument when the code writes words for the user.
2. **`tools/__init__.py`**: import and export the factory (and its client class).
3. **`config.py`**: the settings, in their own `# --- Feature ---` block, off by default
   (`None`, `[]` or `False`); secrets as `SecretStr | None`; CSV lists as `CsvList` and
   added to `_split_csv`. Files it keeps go under `Path(settings.data_dir)`.
4. **`main.py`, `build_registry`**: `registry.extend(build_<feature>_tools(...))`, behind
   the `if settings...` that says it is configured, before the core tools at the end
   (`build_proposal_tools`, `build_long_result_tools`). Something to open and close goes
   in `lifespan`; something to keep doing in the background (an index kept fresh) is a
   coroutine function appended to `registry.jobs`, which `lifespan` runs and cancels.
5. **`prompts.py`**: one `lines.append(...)` in `_daily_help`, behind
   `if "<a tool name>" in t`, saying when to use the tools; add the tool to
   `_PRIVATE_SOURCES` when it has `taints`. A briefing section goes in `BRIEF_SECTIONS`
   (`config.py`) and `_brief_sources`.
6. **`i18n.py`**: every word the code itself says to the user (a `describe` line, a
   message `main` sends), as a field of `Strings`, English first, French kept up to date.
   Text for the model (tool descriptions, `ToolError`, results) stays in English.
7. **`.env.example`**: the settings with a comment, empty or commented out.
8. **`README.md`**: a row in the configuration table, a bullet under "What it does", and
   a bullet under "Security model" when the feature writes, sends data out or reads
   content written by others.
9. **`docs/<feature>.md`** when setting it up takes more than two settings: what it does,
   how to get the credentials, what the agent may and may not do. Link it from the README.
10. **`scripts/setup.py`**: a question in `ask_sources` (or a new `ask_<feature>` called
    from `run`), the keys in `GROUPS` (and in `SECRET_KEYS` when secret).
    `tests/test_setup.py` feeds the answers in order: add yours there.
11. **`tests/test_<feature>.py`**: no network (`respx` for HTTP, `FakeLLM` for the model).
    Cover the handler, each refusal, and through `ToolRegistry.run` that a write parks
    after a read and runs through `run_proposals`. Add the tool names to
    `test_registry_only_has_configured_tools` in `tests/test_api.py` (absent when not
    configured) and to `ALL_TOOLS` in `tests/test_prompts.py`.
12. **`pyproject.toml`** for a new dependency (with a comment saying what needs it), and
    the `Dockerfile` or a file in `compose/` for a system package or a sidecar.

When porting from another codebase: rename the package, replace every example with a
fictional one, translate comments and fixtures to English, move user-facing words to
`i18n.py`, and call the model only through `LLMClient` (`response.content` is a list of
dict blocks, never SDK objects).

## What survives a restart

Everything kept is under `Path(settings.data_dir)`: `journal/`, `conversations/` and
`files/` (chats only: `main.save_conversation`, `restore_conversations`, `file_store`),
`memory.md` and `procedures.md` (unless `MEMORY_STORE=github`), `turned-down-alerts.json`.
`tasks/` (unless `TASKS_STORE=github`), `tasks-done.json`, `tasks-reports.json` and
`task-outbox.json` (which tasks ran, their last reports, what was sent).
A feature that keeps a file follows the same pattern: written through a temporary file,
mode 600, named by the code and never by what a client or a model sent. What must reach
every request's prompt (as the memory does) is added to `main.Kept.prompt_text`, which is
the `memory=` hook of `Agent`; that text is also registered with the leak guard.

## Adding a channel for what the agent says by itself

A task's report leaves through `app.state.delivery` (`deliver.Delivery`). A new channel
(a chat) is a class with the attributes of `deliver.ReportChannel` (`name`, `display`,
`conversation_id`, `reaches`) and `async send(title, text)`. Put it first in
`main.build_delivery` and name it in `main.reports_reach`, which is what turns the
tasks on. With a `conversation_id`, tasks run in that conversation and may park writes
for the user's go there (`main.answer_task`); with `None` (ntfy) nothing is parked.

## Adding an LLM provider

A server that speaks the OpenAI Chat Completions API needs no code: add it to
`OPENAI_COMPATIBLE` in `llm.py` and to `MODELS` in `scripts/setup.py` (a test keeps the two
in step). For another API:

1. In `llm.py`, write a class with `async def create(self, **kwargs) -> LLMResponse`.
   It receives `model`, `max_tokens`, `system`, `tools` (`name`, `description`,
   `input_schema`) and `messages` in the Messages format used throughout the code
   (`text`, `tool_use`, `tool_result` blocks, plus `image` and `document` for files; text
   blocks and tools may carry a `cache_control` marker to drop when the vendor has no
   prompt caching; `tool_choice={"type": "none"}` asks for words only). Convert to the
   vendor's format and back;
   map the stop reason to `end_turn`, `tool_use` or `max_tokens`.
2. Make `build_llm_client` return it, and add its name to `PROVIDERS`. Add it to
   `WEB_SEARCH_PROVIDERS` only if it can run the
   server-side search tool used by `tools/web.py`; otherwise web search stays off.
3. Add a test with a fake vendor client covering a tool call round trip.

## The subscription engine

With `ENGINE=subscription` a turn runs in Claude Code (`engine.ClaudeCodeEngine`), which
calls the tools back one at a time through `Agent._run_tool`: the registry and its flags
decide exactly as on the API, so a tool needs nothing special. Two things to know:

- A model call of the server's own (a search answer, a scan to read, a summary) should go
  to the plan first: pass `SubscriptionFirst(subscription_engine(settings, model), llm)`
  where an `LLMClient` is expected (plain prompts go to the plan, anything with tools or
  files to the API), or call `subscription_first(engine, on_subscription, on_api)` when
  the run needs one of Claude Code's own tools (`builtin=["WebSearch"]`, `["Read"]` with
  `files=`). Those are held by `BuiltinGuard`; never allow another one without a guard
  and a test.
- Without an API key the API client is `engine.NoApi`, whose `create` raises `NoApiKey`:
  a tool that calls the model turns it into a `ToolError`, and a tool that only works on
  the API (it needs `llm.files`, a server-side tool) must stay unregistered then.

Tests use a scripted stand-in for the SDK (`tests/test_engine.py`), so they pass without
the `subscription` extra installed.

## Adding an outdoor activity

Add an `Activity` to `CATALOG` in `activities.py`: kind `weather` with limits (rain,
wind, temperature, visibility) is enough for `get_outdoor_forecast` to handle it. Kinds
`wind` and `surf` reuse the per-spot tools. Add aliases in the languages you know.
