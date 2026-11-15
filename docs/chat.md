# Chat channels

| | |
|---|---|
| **What it does** | A written conversation with the assistant in WhatsApp or Telegram: questions, files, voice messages, one-tap approvals; its briefings, tasks and alerts arrive there. |
| **What you need** | Telegram: a bot you create with @BotFather, nothing else. WhatsApp: a WAHA gateway you run (`compose/waha.yml`), an unofficial client that WhatsApp's terms don't allow. |
| **Settings** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`; `WAHA_API_KEY`, `WHATSAPP_CHAT_ID`; `WHISPER_MODEL` for voice messages; the wizard's question on how you talk to it. All of them: [below](#settings). |
| **Check it works** | Write "hello" in the chat; the agent answers within a few seconds. The startup log says why a channel is off. |
| **Verified** | Run against the real service: WhatsApp, in an earlier form of this code (WAHA, `GOWS` engine). Not run: this version of the WhatsApp code (a simulated WAHA only), `compose/waha.yml`, and Telegram, written from the Bot API's documentation ([below](#not-run-against-telegram)). |

Talk to the assistant in writing from a messaging app: ask it things, send it files and
voice messages, approve what it proposes with a tap, and get its briefings and the tasks
it runs by itself there. Two apps are supported, both off until configured, and both can
run at once:

| | WhatsApp | Telegram |
|---|---|---|
| Goes through | [WAHA](https://github.com/devlikeapro/waha), a gateway you run that links to a number as WhatsApp Web does | a bot you create with @BotFather; the agent calls Telegram's Bot API |
| To run besides the agent | the WAHA container (`compose/waha.yml`) | nothing |
| Official? | no: an unofficial client, which WhatsApp's terms don't allow; an account can be banned | yes, the documented API for bots |
| Who reaches the agent | only the one chat it polls (yours) | only the private chats listed in `TELEGRAM_CHAT_IDS` |
| Its messages ring | no in your own chat; yes with a number of its own | yes |
| What waits for your go | a poll: tick, then "Go" | buttons under a message: tick, then "Go" |
| Files in and out | yes, 10 MB in | yes, 20 MB in (the Bot API's limit) |
| Voice messages | transcribed on the server, when set up | the same |
| Reading your other chats | groups you list, on request | no |
| Run against the real service | an earlier form of this code, yes; this version, not yet (below) | **no** (below) |

`python3 scripts/setup.py` asks which one you want ("how will you talk to it?") and writes
the settings.

## What works the same in both

- Every message you write is a request; the agent answers in the same chat. While it
  works the chat shows it typing; a long request gets a short line now and then, and one
  still running after five minutes says so.
- **Replies and forwards.** A message that replies to another brings that one, quoted as
  data. Replying to one of the agent's own answers that read nothing of others' is your
  own request. A reply to anything else, or a forwarded message, may carry someone
  else's words: what would write or act then waits for your go.
- **What waits for your go** (an email to file after reading it, a plan of several
  steps) is listed by the code as a choice right under the reply. Tick what you want,
  then "✅ Go": exactly those run, as shown; "❌ Cancel" drops the list. Ticking alone
  does nothing. You can also answer in writing ("yes", "yes except 2"). A line too long
  for an option is written out in the message and the option points to it.
- **Questions with a few answers** come as a choice too (`ask_in_poll`): one tap
  answers, with `CHAT_VOTE_GRACE_SECONDS` (5 s) to change your mind before the agent
  acts on it.
- **A reaction** on one of the agent's messages answers it: 👍 is a yes to what it asked.
- **Files** you send (PDF, images, text, Word, Excel, PowerPoint, OpenDocument) are read
  with their caption. A file is content written by others, like an email: no home
  control or web search in that reply, and writes wait for your go. Files the agent
  makes (a PDF, a chart) are sent in the chat.
- **Voice messages** are transcribed on your machine by
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) when `WHISPER_MODEL` is set
  (`small` is a good start; the model, about 500 MB, is downloaded once into the data
  folder) and the `voice` extra is installed (`EXTRAS=documents,voice` in `.env` with
  Docker, then rebuild; `pip install ".[voice]"` without). Only the first five minutes
  are transcribed. Without it, a voice message gets a line asking you to write instead.
  A forwarded voice message is someone else's words: data, never a request.
- **The conversation** is kept for `CHAT_CONVERSATION_TTL_SECONDS` (12 h) and across
  restarts, with its files and what waits. Emails and pages read in a turn are not kept.
  Each channel has its own conversation (`chat-whatsapp`, `chat-telegram`;
  `POST /v1/reset?conversation_id=chat-whatsapp` forgets one).
- **Messages written while the agent was down** are not answered afterwards; one it was
  answering when it stopped gets a line asking you to send it again.
- **What the agent says by itself** goes to the chat first: the tasks it runs
  ([tasks.md](tasks.md)), the briefing and the recap at their hours, pet supplies
  and activity alerts, coding jobs' reports. There you can answer a report and approve what a task proposes. With several
  channels, WhatsApp comes first, then Telegram, then ntfy if the chat can't be reached.
- **From elsewhere into the chat**: `POST /v1/ask`, `/v1/briefing` and `/v1/departure`
  take `"deliver": "chat"` (the first channel) or a channel's name (`"whatsapp"`,
  `"telegram"`): the reply is also sent in the chat and the conversation goes on there.
  That is how a voice Shortcut's briefing can be answered in writing later.
- **The remote browser** ([browser.md](browser.md)): when a site asks you to log in, the
  link to the browser's page comes in the agent's reply, here a tap away; say it's done
  in the chat and it carries on. A capture it takes of a page (`browser_screenshot`) is
  kept by name for a draft, not sent in the chat by itself.
- Phone reminders can't be created from a chat (only a client on the phone can): the
  agent schedules a task instead when tasks are on, and the code says so otherwise.

## WhatsApp

```
your WhatsApp chat  ←→  WAHA (linked device)  ←→  the agent polls that one chat
```

WAHA runs WhatsApp as a linked device of a number, like WhatsApp Web. It is not an
official API: WhatsApp can ban an account that uses one, a new number driven only by a
client like this being the likeliest. Decide with that in mind; Telegram has no such risk.

Two ways to link it:

- **Your own number.** The conversation is your "Message yourself" chat. Nothing rings
  there (every message is from your own account), and the agent's messages start with a
  marker (`WHATSAPP_MARKER`, 🤖 by default) that tells them from yours.
- **A number of the agent's own.** It is then a contact like any other and its messages
  notify. Any number that can receive WhatsApp's code will do. Use it as a person would
  for a few days before linking it. Set `AGENT_WAHA_SESSION` to the session linked to
  that number: the agent then answers only what comes from `WHATSAPP_CHAT_ID`, your
  number; votes and reactions from anyone else who writes to it are ignored.

### Setup

1. `python scripts/setup.py`, choose WhatsApp. With Docker it adds `compose/waha.yml` and
   generates `WAHA_API_KEY` and `WAHA_DASHBOARD_PASSWORD`. By hand:

   ```
   COMPOSE_FILE=docker-compose.yml:compose/waha.yml
   WAHA_API_KEY=<a long random value>
   WAHA_DASHBOARD_PASSWORD=<another one>
   WHATSAPP_CHAT_ID=15550100@c.us      # your number: country code and number, digits only
   ```

2. `docker compose up -d`, then open `http://127.0.0.1:3000/dashboard` on that machine
   (through an SSH tunnel from another: the port is not published). Log in as `admin`
   with `WAHA_DASHBOARD_PASSWORD`, give the API key, start the session `default` and
   scan its QR code with the phone (WhatsApp > Linked devices).
3. Write in the chat. The agent answers within `WHATSAPP_POLL_SECONDS` (5 s).

Without Docker, or with a WAHA you already run, set `WAHA_URL` and `WAHA_API_KEY`
yourself, and `WAHA_SESSION` when the session is not `default`. WAHA must keep the media
of incoming messages for the API (`WHATSAPP_DOWNLOAD_MEDIA=True`, as `compose/waha.yml`
sets it), and its engine must bring poll votes: the code was run with the `GOWS` engine.

Run against the real service: an earlier form of the WhatsApp code (WAHA, `GOWS`
engine), since reworked to fit any installation. Not run: this version, which has only
met a fake WAHA (`tests/test_whatsapp.py`), and `compose/waha.yml`, written from WAHA's
documentation and never started. If the
dashboard or a variable differs in your WAHA version, its own documentation wins.

### What the agent reads

Only the chat `WHATSAPP_CHAT_ID`, from WAHA's local store. Nothing else of the account
leaves WAHA, unless you list groups for summaries:

- `WHATSAPP_READABLE_CHATS`: ids of groups (`...@g.us`, from WAHA's
  `/api/<session>/groups`), comma-separated; a community's id brings its groups, those
  added later included. "Sum up the club's group since yesterday" then reads them
  (`read_whatsapp_chats`): 24 hours back by default, a week at most, 200 messages per
  group, the ten newest voice messages transcribed.
- What people wrote there is theirs: reading it closes the request to home control, web
  search and writes, as an email does. A message in a group can't make the agent act.
- "…and mark them as read" (`mark_whatsapp_chats_read`) sends the blue ticks to the
  group, only when you ask and, after a summary, only on your go. Messages that came
  between the summary and your go are reported, so none is marked read unseen.
- With a number of the agent's own, these groups are still read on your account
  (`WAHA_*`), the chat going through `AGENT_WAHA_*`: two sessions, so two WAHA
  instances or a WAHA edition that runs several.

The volume `waha_sessions` is the link to the account: whoever can read it can act as
that WhatsApp account. Keep the machine and its backups accordingly.

## Telegram

```
your chat with the bot  ←→  Telegram  ←→  the agent asks for news (getUpdates)
```

The agent calls Telegram; Telegram never calls the agent. No public address, no
webhook, nothing else to run.

### Setup

1. In Telegram, talk to **@BotFather**, send `/newbot` and follow it. It gives the bot's
   token: `TELEGRAM_BOT_TOKEN`.
2. Open your bot and send it any message. `python scripts/setup.py` then reads your
   chat's id from Telegram and asks you to confirm it. By hand: start the agent with the
   token and `TELEGRAM_CHAT_IDS=0`, write to the bot, and read the id in the agent's log
   ("Ignored a Telegram update from chat 4242 (not in TELEGRAM_CHAT_IDS)").

   ```
   TELEGRAM_BOT_TOKEN=123456:AA...
   TELEGRAM_CHAT_IDS=4242
   ```

3. Restart the agent and write to the bot.

### Who can talk to it

Anyone can find a bot by its name and write to it. The agent drops, before reading it,
everything that is not a **private chat listed in `TELEGRAM_CHAT_IDS`, written by that
chat's own user**: messages, taps on buttons, reactions. A group the bot is added to
reaches nothing, even with you in it. The bot never answers a stranger, not even to
refuse. Several ids (comma-separated) are several people, each with a conversation of
their own (`chat-telegram`, then `chat-telegram-<id>`), but one agent, one memory and one
home: list only people you would give the agent's token to. What the agent says by
itself goes to the first id.

The token is all it takes to read what is sent to the bot and to write as it: it stays
in `.env`, and the code keeps it out of the logs and of error messages.

### How it differs from WhatsApp

- Choices are buttons. A tap ticks a button (☑), another tap unticks it; what is ticked
  is kept in the message itself, so buttons still work after the agent restarts.
- `*bold*` in a reply is sent as real bold; no other formatting is interpreted, so no
  character of a reply can break a message. Long replies are split.
- The agent marks a message it failed on with 🤷, and a choice it acted on with 👍
  (Telegram lets bots use a fixed list of reactions).
- `/start`, which Telegram sends when you open a bot, is not a request and gets no answer.
- A bot that already has a webhook set elsewhere can't be polled (Telegram answers 409):
  use a bot made for the agent.
- `TELEGRAM_API_URL` points to another server speaking the Bot API (a self-hosted
  `telegram-bot-api`), if you run one.

### Not run against Telegram

The Telegram channel was **written from the Bot API's documentation and tested only
against a fake of that API** (`tests/test_telegram.py`). Nobody has run it against
Telegram itself yet. The calls it makes are `getUpdates`, `sendMessage`, `sendPhoto`,
`sendVideo`, `sendDocument`, `getFile`, `sendChatAction`, `answerCallbackQuery`,
`editMessageReplyMarkup` and `setMessageReaction`. Expect small surprises on first use
(a field named differently, a limit), and please report them. The security rule above
(who can talk to it) does not depend on any of that: it is checked on every update
before anything else looks at it.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `WAHA_URL`, `WAHA_API_KEY`, `WAHA_SESSION` | unset, unset, `default` | The WAHA gateway linked to your number (`compose/waha.yml` sets the URL). |
| `WHATSAPP_CHAT_ID` | unset | Your number as `<digits>@c.us`: the one chat the agent talks in. WhatsApp is on with this and WAHA. |
| `AGENT_WAHA_SESSION`, `AGENT_WAHA_URL`, `AGENT_WAHA_API_KEY` | unset | A session linked to a number of the agent's own; URL and key default to `WAHA_*`. |
| `WHATSAPP_MARKER` | `🤖` | What the agent's WhatsApp messages start with. |
| `WHATSAPP_POLL_SECONDS` | `5` | How often the chat is read. |
| `WHATSAPP_READABLE_CHATS` | empty | Groups and communities the agent may sum up on request. |
| `TELEGRAM_BOT_TOKEN` | unset | The bot's token from @BotFather. |
| `TELEGRAM_CHAT_IDS` | empty | The private chats that may talk to the bot. Telegram is on with both. |
| `TELEGRAM_API_URL` | `https://api.telegram.org` | Another Bot API server. |
| `CHAT_VOTE_GRACE_SECONDS` | `5` | How long a one-tap answer can still be changed. |
| `WHISPER_MODEL`, `WHISPER_LANGUAGES` | empty, `LANGUAGE` | Voice messages transcribed on this machine (`voice` extra); the languages you speak in them. |

## Adding another app

A channel is a class with the methods of `ChatChannel` (`src/intelligent_home_agent/chat.py`):
receive the user's messages with their files and the message they reply to, send text
and files, send a choice and report what is ticked, and optionally show typing and
marks. `chat_session.py` does the rest for every channel (the agent, what waits for a
go, votes, plans). Build yours in `main.build_channels`; `whatsapp.py` and `telegram.py`
are the two examples, and `tests/test_chat_session.py` shows a channel in twenty lines.
The one rule a channel must keep: only the user reaches the handler.
