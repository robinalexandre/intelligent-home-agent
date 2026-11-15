# Memory

| | |
|---|---|
| **What it does** | Keeps what you tell it to remember, the steps of jobs that worked, the alerts you turned down, a journal of requests it can search, and a chat across restarts. |
| **What you need** | Nothing: files in `DATA_DIR` by default. Optional: a GitHub repository of yours for the two memory files. |
| **Settings** | `MEMORY_STORE`, `MEMORY_REPO`, `JOURNAL_KEEP_DAYS`, `FILES_KEEP_DAYS`; the wizard's `memory` item. All of them: [settings.md](settings.md#memory-and-knowledge). |
| **Check it works** | Say "remember that my accountant is Jo Martin", then in a new conversation "who is my accountant?"; the line is in `memory.md`. |
| **Verified** | Ported from the private installation this project comes from; which parts ran there against the real service is not recorded on this page. In this repository: the test suite only, against simulated services. |

What the agent keeps from one conversation to the next, and where.

| What | Tools | Where |
|---|---|---|
| Facts about you ("my accountant is Jo Martin", "I take the train on Mondays") | `remember`, `forget` | `memory.md` |
| Steps of a job that worked (a bill fetched from a provider's site, a booking) | `find_procedure`, `save_procedure` | `procedures.md` |
| Alerts you turned down ("I know", "stop with that") | `turn_down_alert`, `list_turned_down_alerts`, `bring_back_alert` | `DATA_DIR/turned-down-alerts.json` |
| What was asked and answered, day by day | `read_day_journal`, `search_past_conversations` | `DATA_DIR/journal` |
| A chat in progress, its files and what waits for your yes | none: the API does it | `DATA_DIR/conversations`, `DATA_DIR/files` |

It works without any setup: by default everything is a file in `DATA_DIR` (the
`agent_data` volume in Docker). The first two files can live in a GitHub repository
instead.

## Facts: `memory.md`

Tell the assistant "remember that my accountant is Jo Martin" and it adds one dated line:

```markdown
# Nova — long-term memory

Durable facts Nova keeps about you, read at every request. One fact per line starting with "- "; edit or delete lines freely.

- My accountant is Jo Martin, jo@example.com (2026-03-02)
- I take the train on Mondays (2026-03-09)
```

Every request then starts with these lines (the newest 6,000 characters of them). "Forget
what you know about my accountant" removes the lines with those words. The file is
yours: edit it, reorder it, delete lines. Only lines starting with `- ` are read.

Rules the code enforces:

- A fact comes back in every later request, so an email or a web page must never plant
  one. After the request read anything written by others (mail, calendar, a file, the
  web, a connected service), or when your message quotes another one, `remember`,
  `forget` and `save_procedure` do not run: they wait for your yes, shown with every word
  under the reply.
- Passwords, keys and tokens are refused.
- The model is told these are facts you gave, not instructions.

## Procedures: `procedures.md`

When a job of several steps worked, the assistant can keep how it did it, one section per
site and job, and look it up the next time instead of searching again. Saving the same
site and job again replaces the old steps. Nearly always the job itself read pages, so the
steps wait for your yes, every line shown.

## Where the two files live: `MEMORY_STORE`

| `MEMORY_STORE` | Where | Needs |
|---|---|---|
| `local` (default) | `DATA_DIR/memory.md`, `DATA_DIR/procedures.md` | nothing |
| `github` | `<assistant>/memory.md`, `<assistant>/procedures.md` in a repository | `MEMORY_REPO`, `GITHUB_TOKEN` |
| `off` | nowhere: no memory, no procedures | |

`MEMORY_PATH` and `PROCEDURES_PATH` change the file names. `<assistant>` is
`ASSISTANT_NAME` in lower case ("Nova" gives `nova/`, "Mrs. Hudson" gives `mrs-hudson/`).

### Local files

Nothing to configure. To read or edit the file with Docker:

```bash
docker compose exec agent cat /app/data/memory.md
```

Back up the `agent_data` volume if you care about what it holds.

### A GitHub repository

Use this to read and edit the memory from any computer, with git's history of every
change. Any repository you own works; make it **private**, as the file is about you.

```bash
MEMORY_STORE=github
MEMORY_REPO=you/your-notes
GITHUB_TOKEN=github_pat_...     # fine-grained, that repository, Contents: read and write
```

- The token is the same `GITHUB_TOKEN` the pull-request tool uses. A classic token with
  the `repo` scope does both; a fine-grained token limited to this repository is safer but
  then sees no pull requests elsewhere.
- The agent creates the files on the first fact. Each change is one commit
  (`chore(memory): Nova remembers a fact`). An edit you push meanwhile is never
  overwritten: the agent reads again and applies its change on top.
- When GitHub can't be reached, the request goes on without the memory, and `remember`
  says it failed.
- One way to already have such a repository: the config sync of the public
  [claude-code-kit](https://github.com/robinalexandre/claude-code-kit) keeps a private
  repository of your coding-assistant settings and notes across your computers. Pointing
  `MEMORY_REPO` at it puts the assistant's memory beside them (and
  [the knowledge base](knowledge.md) can index the same repository).

### Optional: a repository encrypted with git-crypt

If the repository encrypts some folders with [git-crypt](https://github.com/AGWA/git-crypt)
(a `.gitattributes` line such as `nova/** filter=git-crypt diff=git-crypt`), the GitHub API
only sees ciphertext. Give the agent the repository's key and it reads and writes the
files in git-crypt's own format, so your unlocked clones see plain text as usual:

```bash
git-crypt export-key /tmp/key && base64 -w0 /tmp/key && rm /tmp/key   # macOS: base64 -i
MEMORY_CRYPT_KEY=<that one line>
```

- Needs the `cryptography` package: it is in the Docker image; without Docker,
  `pip install ".[crypt]"`.
- Which folders are encrypted is read from the repository's `.gitattributes` at startup.
  If it can't be read, the agent's own folder counts as encrypted. A file found encrypted
  is always written back encrypted.
- With a key that can't be used (not a git-crypt key, or the package missing), the memory
  is off and the log says why: the agent never falls back to writing in clear.
- Without a key, an encrypted file is left alone: nothing is read, nothing is written.
- Only symmetric keys (`git-crypt export-key`) are supported, not GPG users. The key
  decrypts every encrypted file of that repository: it is as sensitive as their contents.

## Alerts you turned down

A briefing can raise the same thing day after day. Say "I know" or "stop with that" and
the assistant keeps one line; the list is given to every request with the memory, and
those alerts are left out. "What did I turn down?" lists them, "tell me about that parcel
again" brings one back. It only makes the assistant quieter, so it works in any request.

## Past conversations

The journal (`JOURNAL_KEEP_DAYS`, a year by default) keeps one line per request: what was
asked, the tools used and the reply. `search_past_conversations` finds the requests and
replies that contain given words; `read_day_journal` gives one day in order.

- A request that read mail, a file or the web kept no reply in the journal, only your
  question and the tools' names: the search shows it as such.
- A journal entry that quoted another message or came with a file may repeat others'
  words: reading it taints the request, like the email itself would.
- `JOURNAL_KEEP_DAYS=0` turns the journal and both tools off.

## A chat across restarts

A spoken conversation lasts a few minutes and lives in memory only. A chat
(`"display": "chat"` in `/v1/ask`) is answered hours later, so after each of its requests
the agent saves it in `DATA_DIR/conversations`: what was said (already without the
private content of tainted requests) and the actions waiting for your yes. At startup the
conversations still within `CHAT_CONVERSATION_TTL_SECONDS` come back; the others are
deleted.

The files sent in a chat or made by the agent in it are kept in `DATA_DIR/files` for
`FILES_KEEP_DAYS` (30) days, stored under a hash of their content, never under their name.
They outlive the conversation: the same `conversation_id` finds them again.
`FILES_KEEP_DAYS=0` keeps files in memory only.

`POST /v1/reset?conversation_id=...` forgets a conversation with its saved copy and its
files. Pending Home Assistant confirmations (locks, alarms) are never saved: after a
restart, ask again.

## What is not there

- No automatic memory: the assistant does not decide on its own to remember what it read.
- One memory per agent: there are no separate users.
- Local files are not encrypted by the agent; protect the disk or the volume.
