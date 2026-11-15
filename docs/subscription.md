# Claude on your Claude subscription

| | |
|---|---|
| **What it does** | Claude answers on your own Claude plan (Pro, Max...) through Claude Code, instead of the API billed by use. |
| **What you need** | `LLM_PROVIDER=anthropic`, a token from `claude setup-token`, the `subscription` extra (some 400 MB). An API key as well is optional. |
| **Settings** | `ENGINE=subscription`, `CLAUDE_CODE_OAUTH_TOKEN`, `SUBSCRIPTION_MODEL`, `SUBSCRIPTION_DISPLAYS`; the wizard's "claude-subscription" model choice. |
| **Check it works** | The startup log says who answers what, e.g. `engine: Claude subscription (...) for chat, the API (...) for the rest`. |
| **Verified** | Not run against the real service yet: the test suite only, where the SDK is replaced by a scripted stand-in. |

Claude can answer in two ways. With an API key (`LLM_PROVIDER=anthropic` and
`LLM_API_KEY`), every reply is a request to Anthropic's API, billed by use. With
`ENGINE=subscription`, replies are written by [Claude Code](https://claude.com/claude-code)
running inside the agent, signed in to your own Claude plan (Pro, Max...): they draw on the
plan you already pay for instead of API credit.

It is off by default and only exists with `LLM_PROVIDER=anthropic`. The wizard offers it as
"claude-subscription" in its model question.

## What you gain and what you lose

| | API key | Subscription |
|---|---|---|
| Cost | per request | included in your plan, within its usage limits |
| Time before a reply starts | a second or two | a few seconds more: a Claude Code process starts for each turn |
| Good for | everything, voice included | a chat, a briefing; slow for a voice assistant |
| A file you send (PDF, photo) | read | not read: the API takes that turn, if there is a key |
| Limits | your API credit | your plan's limits, shared with your own Claude Code sessions, by windows of a few hours |
| Code sandbox (`CODE_EXECUTION_ENABLED`) | yes | API only |

This is your own plan, used by your own agent, for yourself. Anthropic decides what a
subscription may be used for and can change it: read your plan's terms, and use an API key
for anything you share with other people.

## Setting it up

1. On a computer with a browser, install Claude Code and run:

   ```bash
   claude setup-token
   ```

   It opens the browser to sign in to your Claude account and prints a long-lived token
   (valid a year). That token is a login to your plan: keep it like a password.

2. In `.env`:

   ```bash
   LLM_PROVIDER=anthropic
   ENGINE=subscription
   CLAUDE_CODE_OAUTH_TOKEN=<the token>
   # Optional, see "With or without an API key" below.
   LLM_API_KEY=
   ```

3. Install Claude Code's SDK with the agent. It is an optional part (the `subscription`
   extra, some 400 MB, since it brings Claude Code itself):

   - With Docker: `EXTRAS=documents,subscription` in `.env` (add it to what `EXTRAS`
     already lists), then `docker compose up -d --build`.
   - Without Docker: `pip install ".[subscription]"`.

   Without it the feature is simply absent: with an API key the agent starts, answers on
   the API and logs what to install; without a key it refuses to start and says the same.

The startup log says who answers what, e.g.
`engine: Claude subscription (claude-sonnet-5-5) for chat, the API (claude-sonnet-5-5) for the rest`.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `ENGINE` | `api` | `subscription`: Claude Code on your plan answers. |
| `CLAUDE_CODE_OAUTH_TOKEN` | unset | The token of `claude setup-token`. Required with `ENGINE=subscription`. |
| `SUBSCRIPTION_MODEL` | `LLM_MODEL` | The model the subscription answers with, when it differs from the API's. Your plan decides which models it may use. |
| `SUBSCRIPTION_DISPLAYS` | `chat` | Where the subscription answers, among `voice`, `notification` (the briefing) and `chat`; the API answers the others. Ignored without an API key. |

A client chooses the display in its request (`"display": "chat"`); the Siri Shortcut and
Home Assistant Assist are `voice`, the briefing endpoint is `notification`. With the
default, spoken requests stay on the API, where the wait is short;
`SUBSCRIPTION_DISPLAYS=voice,notification,chat` moves everything to the plan if a few
seconds of silence before each answer are fine for you.

## With or without an API key

**With `LLM_API_KEY` as well** (recommended): the subscription answers where
`SUBSCRIPTION_DISPLAYS` says, and the API takes over whenever it can't:

- a message that comes with a file to read;
- a usage limit of your plan: the subscription is then left alone for half an hour;
- any error (Claude Code missing or failing): left alone for five minutes.

A turn is never done twice: if the subscription fails after one of the agent's tools
already ran, that request fails rather than start again on the API.

**Without an API key**: the subscription answers every display, and there is nothing to
take over. For a file you send, or during a usage limit or an error, the agent replies with
one sentence that says so, and nothing is lost: the file is kept by name, the conversation
goes on. Web search and scanned PDFs on a drive work through the subscription alone, and
fail with a clear message while it is unavailable. The code sandbox is not offered.

Web search (`WEB_SEARCH_BACKEND=native`, the default with Claude) and the reading of
scanned PDFs on a drive (`DRIVE_OCR=model`) also go to the subscription first once
`ENGINE=subscription` is set, on the small models they already use, and to the API when it
can't take them.

## What Claude Code may do here

Nothing of its own. It is used as the model and nothing more:

- Its own tools (shell, file edits, its agents) are all off. Its system prompt is the
  agent's. No settings file, memory file or project instruction is read.
- The agent's tools reach it through a private channel whose handlers are the agent's own
  tool registry, so every rule holds exactly as on the API: content written by others
  taints the request, writes after it wait for your go, sensitive actions need a later
  yes, and what leaves is checked against what the request holds.
- Each turn is a fresh run that receives the conversation as text. Claude Code keeps no
  memory between turns: what the agent drops after a reply (emails, pages) stays dropped.
- Three of Claude Code's tools are allowed in runs the server starts for itself, each held
  by a check in code. A web search run gets `WebSearch` and sees only the query. Reading
  a scan gets `Read`, limited to that one file in a folder made for the run and removed
  after. `WebFetch`, where a run is given it, only opens addresses already in that run
  (its prompt, its search results), a limited number of them. None of these runs has the
  agent's tools or the conversation.
- It can never bill an API key by accident: the run is started without one, and a run that
  reports another source of credentials than the login is refused and turns the
  subscription off until the agent restarts.

Claude Code's own files (its config and the transcripts of its runs) are kept in
`DATA_DIR/claude`, on the agent's data volume, never in your own Claude Code folder. Those
transcripts hold what the turns read: protect and back up that folder as you do the rest
of `DATA_DIR`.

## When it doesn't answer

- `ENGINE=subscription needs the Claude Agent SDK`: step 3 above.
- `Engine unavailable, the API takes this turn: ...` in the log: the reason follows (a
  usage limit, a sign-in that expired: run `claude setup-token` again).
- `Claude Code would bill an API key`: something in the agent's environment gives Claude
  Code other credentials; remove it and restart.
