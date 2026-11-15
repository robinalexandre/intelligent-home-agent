# Nightly review

Every night, a coding job can read what you asked the agent the day before, fix in the
code what made it fail or take the long way round, and open a pull request for you to read
before merging. Off by default.

```
each request → DATA_DIR/journal/<day>.jsonl → at REVIEW_AT: a quiet coding job on REVIEW_REPO
                                                  → a pull request there, or nothing
```

## Read this first: what the coding job sees, and where it goes

The journal holds your private conversations with the agent. The review hands one day of
it to a coding job, so before turning it on, know exactly what leaves the agent:

- **What is in the brief**: for each request of the day, the time, what you asked (your
  words, up to 1 500 characters), the names of the files you sent, the tools
  called with their arguments and errors, how long it took, and the reply (up to 2 000 characters). For a request
  that read others' content (an email, a file, a web page), only the tools' names and
  errors: neither their arguments nor the reply, since those could repeat that content.
  Then the coding jobs of that day: their requests and last reports. At most about 80 000
  characters; on a longer day the requests that went wrong are kept first.
- **Who receives it**: the coder. With `JOBS_RUNNER=claude_code`, Claude Code in your own
  coder container, which sends it to Anthropic under your Claude subscription, like
  anything you type in Claude Code; the brief also stays in the coder's volume
  (`coder_work`, in the job's file and Claude Code's transcript). With
  `JOBS_RUNNER=managed`, an Anthropic Managed Agents session on your API account.
  Nothing else receives it: no other service, and never the public repository.
- **What comes out**: a branch and a pull request on `REVIEW_REPO`, and the job's final
  message, which reaches you only as a line with the pull request's address. The brief
  tells the coder that the journal is private and that nothing personal may go into the
  code, the tests, the commits or the description. That is an instruction to a model, not
  a guarantee: **read the pull request before you merge it or show it to anyone**.
- **So `REVIEW_REPO` must be yours, and private.** The agent asks GitHub; a repository it
  is not told is private gets nothing, and the night is skipped with a line in the log.
  `REVIEW_ALLOW_PUBLIC=true` lifts that check, for a repository you made public on
  purpose. Note that GitHub's "Fork" button on a public repository makes a public fork:
  for a private copy, create an empty private repository and push a clone to it
  (`git clone --bare`, then `git push --mirror` to yours), or use GitHub's import.
- The review never opens a pull request on the public repository this project comes
  from. If a fix would help everyone, propose it there yourself, once you have checked it
  holds nothing of yours.

Requests kept in the journal for less time, or not at all: `JOURNAL_KEEP_DAYS` (`0` turns
the journal off, and the review with it).

## The review

`review.py` checks every few minutes; once `REVIEW_AT` (local time) has passed, it hands
the day before's journal and that day's coding jobs to a coding job on `REVIEW_REPO`. The
coder looks for, in order:

1. requests the agent could not do, did wrong, or that ended in an error or a fallback;
2. requests done only after needless questions or confirmations although your own words
   already agreed;
3. replies too long for where they were read.

It works out each cause from the code and the prompt and fixes it: a prompt rule, a tool's
description, a tool, or a lighter path for that kind of request. It never loosens a
guardrail on others' content (taint, parked writes, consent before deleting or moving,
leak checks). It opens one pull request, on the branch `fix/nightly-review/<day>` of
`REVIEW_REPO`, whose description takes each change in turn: what happened, why the agent
got it wrong, what the change does, how it was tested.

The job is quiet: it never stops to ask, and you hear of it only when it opened a pull
request, in one line with its address, the way the agent's other reports reach you. A day
without requests starts no job; a night is marked reviewed (`DATA_DIR/review-done.json`)
before its job starts, so a failure is not retried in a loop.

Merging the pull request changes your copy. To run the changed code, deploy your copy
(`git pull` on the server, `docker compose up -d --build`); to keep up with the public
repository, merge it into your copy from time to time.

## Lessons for your knowledge base (optional)

The job's final message can end with lessons worth remembering beyond the fix (a trap in
a tool, a cause that took long to find), written without personal data. With
`REVIEW_LESSONS_REPO`, the server curates them right away into the notes repository your
[knowledge base](knowledge.md) indexes: the curator of the public
[claude-code-kit](https://github.com/robinalexandre/claude-code-kit/tree/main/skills/kb)'s
`kb` tool reads them with the notes of the `knowledge/` folder (`REVIEW_LESSONS_PATH`) and
decides what becomes a note, with trust `agent` and `nightly-review` as its source; a note
you wrote yourself is never changed. New and completed notes, and a report
`knowledge/.curator/<day>-nightly-review.md`, are written back; the kb container finds
them at its next pull.

- It needs the `curation` extra: `EXTRAS=documents,curation` in `.env` with Docker (then
  rebuild), or `pip install ".[curation]"`. Without it, nothing is curated and the log
  says so.
- `GITHUB_TOKEN` must be allowed to write that repository's contents.
- The curator's model call (the lessons and the titles of your notes) goes to the agent's
  own model: your provider's API, or your Claude subscription first with
  `ENGINE=subscription`.
- A notes folder encrypted with git-crypt is read and written when the repository is the
  one of `NOTES_REPO` or `MEMORY_REPO` and the agent has its key.

## Setup

The coding jobs must be on ([jobs.md](jobs.md)), with a GitHub token that can push to
`REVIEW_REPO`. Then in `.env`:

```bash
REVIEW_REPO=you/your-private-copy
REVIEW_AT=05:30
```

The startup log says `nightly review: on you/your-private-copy, after 05:30`.

| Setting | Default | What it does |
|---|---|---|
| `REVIEW_REPO` | unset | Your own private copy of this repository, `owner/repo`. Unset: no review. |
| `REVIEW_AT` | `05:30` | Local time after which the day before is reviewed. |
| `REVIEW_ALLOW_PUBLIC` | `false` | Hand the journal to a repository that is not private. |
| `REVIEW_LESSONS_REPO`, `REVIEW_LESSONS_PATH` | unset, `knowledge` | Where the lessons are curated into notes. |
