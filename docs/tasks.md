# Scheduled tasks and the notes repository

Two things that go together but work apart:

- **Tasks** the assistant starts by itself: a reminder at a time, a check every morning,
  the briefing at 07:30. No account needed.
- **A notes repository** on GitHub: reminders and handoff notes for your coding sessions,
  which the assistant reads and, if you let it, writes. Optional.

## Tasks

Ask "remind me tomorrow at ten to call the plumber", "every Monday, tell me which
invoices are unpaid" or "check twice a day whether my parcel moved". The assistant writes
a task; every minute the agent looks at the tasks and runs the ones that are due, as if
you had just sent the request. The reply goes out as a push notification.

### What it needs

A way to reach you when you asked nothing: **`NTFY_URL`** ([ntfy](integrations.md), a
free push app). Without it there are no tasks and no task tools. Nothing else to set up.

Every report is also kept (the last 50) and served by `GET /v1/tasks/reports`, with
`?since=<the "at" of the last one you have>` for the newer ones only. A client that
prefers fetching to being pushed, such as a Home Assistant automation that reads the
report aloud, can use that.

A chat channel, when one is configured, takes the reports instead of ntfy; in a chat you
can also answer a report ("yes" to what it proposes). A push can't be answered, so a
task that runs through ntfy only reads and reports: what would need your go is left out.

### The briefing and the recap by themselves

```bash
MORNING_BRIEFING_AT=07:30
EVENING_RECAP_AT=19:00
```

Each becomes a task that runs every day at that time (in `TIMEZONE`). They follow the
same sections as when you ask for them (`BRIEF_SECTIONS`). The other repeating tasks due
at the same minute are folded into the briefing rather than sent one by one. This
replaces a cron job calling `POST /v1/briefing`; that endpoint still works.

### Where tasks are kept: `TASKS_STORE`

| `TASKS_STORE` | Where | Needs |
|---|---|---|
| `local` (default) | `DATA_DIR/tasks`, one Markdown file per task | nothing |
| `github` | a folder of the notes repository, `<assistant>/tasks` | `NOTES_REPO`, `GITHUB_TOKEN` allowed to write its contents |
| `off` | nowhere: no tasks | |

`TASKS_PATH` changes the folder. `<assistant>` is `ASSISTANT_NAME` in lower case ("Nova"
gives `nova/tasks`).

With `local`, the assistant's own tools are how tasks are made, listed, changed and
removed; you can also edit the files (`docker compose exec agent ls /app/data/tasks`).
With `github`, you and your coding assistant can drop a task file in the folder from any
computer, and git keeps the history.

### A task file

```markdown
---
title: Unpaid invoices
on: 2026-11-16 08:30
every: monday
until: 2027-01-31
---

List the invoices still unpaid and who to chase first.
```

| Field | Meaning |
|---|---|
| `title` | One line; it is the title of the notification. |
| `on` | When it starts: `YYYY-MM-DD HH:MM` or `YYYY-MM-DD`. For a repeating task, its time of day (09:00 without one). |
| `until` | Last day it may run (optional). |
| `every` | Repeats: `day`, `weekday`, week days (`monday, thursday`), `3 days` (counted from `on`), `month` (on `on`'s day). Without it, the task runs once. |
| `times` | A repeating task several times a day: `08:00, 14:00`. |
| `digest` | `yes`: gathers the other repeating tasks due with it into its one message (the briefing). `no`: never folded into one. |
| `fresh` | `yes`: see below. |
| `done`, `closed` | Written by the agent. `done: 2026-11-16` closes a repeating task for that day; set it yourself to skip today. |

The body is the request, written as you would say it. Only simple `key: value` lines are
read.

What happens when it runs:

- A task that runs **once** moves to `past/` with a `closed:` line.
- A **repeating** task stays, and may stay silent: it sends nothing when there is nothing
  new. It gets its previous reports back so it only says what changed.
- A task that fails is logged and you are told it failed; it is never run twice.
- A time missed while the agent was off: a one-off task runs at the next start (until
  `until`); a repeating one runs once if it is still the same day.

### Repeating tasks and the web: `fresh`

A repeating run carries its last report, which came from what an earlier run read
(emails, a web page). So it is treated like a request that read others' content: it can
read and report, but web searches and writes do not run in it. That is deliberate: a
task that runs unattended every day is where a planted instruction would do most harm.

A task that needs a web search each time (a tech watch, "any news about X") runs
*fresh* instead: clean, without its last report, only told when it last ran. The agent
switches a task to fresh runs by itself the first time a run is refused a search; write
`fresh: yes` to start that way, or `fresh: no` to forbid it.

### What the assistant may do with tasks

- `schedule_task` only runs in a request that read nothing written by others. After the
  request read an email or a page, the task waits for your yes, shown word for word: an
  email can't plant a task.
- Changing or deleting a task takes your own message, never a task or a plan.
- Keys and tokens are refused in a task.
- A task can't do more than you can ask: the same rules apply to what it reads and
  writes.

## The notes repository

A private GitHub repository holding notes for your coding sessions:

```bash
GITHUB_TOKEN=github_pat_...   # Contents: read (and write, for NOTES_WRITE or TASKS_STORE=github)
NOTES_REPO=you/your-notes
NOTES_WRITE=true              # optional
```

| Folder | What | The assistant |
|---|---|---|
| `reminders/` (`ASSISTANT_REMINDERS_PATH`) | Reminders for your coding sessions, one file each, same format as a task file plus `where`, `machine`, `group` | reads the due ones (`get_assistant_reminders`, also in the briefing); with `NOTES_WRITE`: leaves one, marks one done, deletes one |
| `handoffs/` (`HANDOFFS_PATH`) | Handoff notes: the goal, where a piece of work stands, the next steps | with `NOTES_WRITE`: writes one, deletes one |
| `<assistant>/tasks/` | Tasks, with `TASKS_STORE=github` | see above |

So you can say "leave a note for my next coding session: finish the checkout page, the
payment form still fails on Safari", and find it on your computer.

One way to get such a repository, with something that reads it: the config sync of
[claude-code-kit](https://github.com/robinalexandre/claude-code-kit) keeps a private
repository of coding-assistant settings and notes across your computers, and its
reminders and handoff skills show these files at the start of a session. The formats here
are the kit's, so the two work together without any setting. It is not required: the
files are plain Markdown, and any tool, script or person can read and write them.
`ASSISTANT_REMINDERS_REPO` is the former name of `NOTES_REPO` and still works.

Rules:

- Marking a reminder done, deleting one, deleting a handoff note: always shown to you
  first, and done on your yes.
- Leaving a reminder or a handoff note after the request read others' content also waits
  for your yes, every word shown.
- A closed reminder moves to `reminders/past/` with a `closed:` line; a repeating one
  gets `done: <today>` and comes back on its next day.
- Each note is a new file and one commit (`chore(sync): add reminders/... from nova`); the
  agent never overwrites a note.

### Reading your repositories

```bash
GITHUB_REPOSITORIES=true
```

Read-only tools over what the token can see: `list_repositories`, `browse_repository`
(a folder or a text file), `search_repository`, and `check_dependencies`, which reads a
repository's lockfiles (npm, pnpm, Composer, Poetry, uv, Pipfile, pinned requirements)
and asks [OSV.dev](https://osv.dev) which exact versions have a known flaw or are
malicious. Only your own repositories are checked. The names and versions of the packages
are sent to OSV.dev; nothing else leaves. With a notes repository, it is where the
assistant looks first for anything about your projects.

A classic token with the `repo` scope sees every repository of yours. A fine-grained
token limited to the notes repository keeps the assistant to that one.

### A repository encrypted with git-crypt

As for [the memory](memory.md#optional-a-repository-encrypted-with-git-crypt): give the
repository's exported key and the agent reads and writes reminders, handoff notes and
tasks in git-crypt's own format.

```bash
NOTES_CRYPT_KEY=<git-crypt export-key, base64, one line>
```

- Not needed when the memory is in the same repository and `MEMORY_CRYPT_KEY` is set.
- Which folders are encrypted comes from the repository's `.gitattributes`. If it can't
  be read, every folder the agent writes to counts as encrypted. A file found encrypted
  is written back encrypted.
- Without a key, an encrypted file is left out, never shown as noise. With a key that
  can't be used, the notes tools and the repository's tasks are off, and the log says so.

## What is not there

- No task without a push channel (`NTFY_URL`), and no answer to a push: what needs your
  yes is not parked by a task sent through ntfy.
- A file the assistant made during a task is not attached to the notification.
- Seconds: tasks are checked once a minute (`TASKS_POLL_SECONDS`).
- One list of tasks per agent: there are no separate users.
