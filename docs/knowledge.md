# Knowledge base

| | |
|---|---|
| **What it does** | Searches your own Markdown notes (by text, by meaning, by links) before the agent says it doesn't know. |
| **What you need** | Docker, a GitHub repository with a `knowledge/` folder of notes, a token that reads it, `compose/kb.yml`. |
| **Settings** | `KB_REPO`, `KB_GITHUB_TOKEN`, `KB_TOKEN` (`KB_URL` is set by the Compose file); the wizard's `knowledge` item. |
| **Check it works** | `docker compose logs -f kb` shows `index ready` and `recall` is in the startup log's tools; ask what it knows about something you wrote down. |
| **Verified** | Not run against the real service yet: the test suite only, against simulated services. `compose/kb.yml` was not started. |

The agent can search your own notes before it says it doesn't know: what you decided, how
your projects, servers and tools are set up, people and companies, lessons from your coding
sessions.

```
your notes (Markdown in a GitHub repository) ──git pull every 5 min──► kb container
you ─► agent ─ recall / related_notes / read_note ─────────────────────► its index
                                                              (text, meaning, links)
```

The index is the `kb` tool of the public
[claude-code-kit](https://github.com/robinalexandre/claude-code-kit/tree/main/skills/kb),
built from that repository by `compose/kb.yml`. It indexes the notes three ways: full text
with accents folded, multilingual embeddings computed locally (nothing is sent to a model
provider), and the graph of `[[links]]` between notes.

## Where the notes come from

Any GitHub repository with a `knowledge/` folder of Markdown notes works. Two ways to fill it:

- **By hand**: one file per note, with a short front matter.

  ```markdown
  ---
  title: Where the backups go
  type: fact          # fact, decision, howto, reference, event, person, project, feedback, user
  trust: user         # user: you said it. agent: an assistant concluded it. third-party: someone else wrote it.
  topics: [backups, nas]
  ---

  Nightly to the NAS, weekly to the cloud bucket. See [[restore-procedure]].
  ```

- **With claude-code-kit**, if you use Claude Code: its config sync keeps a private
  repository of your settings and notes across your computers, and its `kb` skill curates
  notes from your coding sessions every morning (`kb curate`). Point `KB_REPO` at that
  repository and the agent knows what your coding assistant learned. Install it from the
  kit's README; nothing else is needed on the agent's side.

## Setup

The wizard (`scripts/setup.py`) offers it as `knowledge` in its menu, with Docker. By hand,
in `.env`:

```bash
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:compose/kb.yml      # after the files you already have
KB_REPO=you/your-notes                               # owner/repo
KB_GITHUB_TOKEN=github_pat_...                       # fine-grained, Contents: read on that repository
KB_TOKEN=...                                         # python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then `docker compose up -d --build`. The first start clones the notes and downloads the
embedding model (about 500 MB, kept on the volume); `docker compose logs -f kb` shows
`index ready`. Ask the agent what it knows about something you wrote down.

The index is a cache: removing the `kb_data` volume loses nothing but a few minutes of
rebuild. Without Docker, run `kb serve` from the kit yourself and set `KB_URL` and
`KB_TOKEN`.

## Trust

- The agent's tools only read notes. They are written by you or by your tools, and reach
  the container through the repository.
- `recall` leaves `trust: third-party` notes out unless asked for them, and then the
  request counts as having read someone else's content, like after an email: home control
  and web search are off for the rest of it.
- A note written by the kit's curator is shown to the agent as `written_by: curator`: a
  model's reading of a coding session, to be weighed, not your word.
- The container has its own GitHub token, read-only on the notes, and none of the agent's
  secrets. The agent reaches it with `KB_TOKEN` on an internal network with no way out.
