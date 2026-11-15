# Coding jobs

For people who code. Ask the agent for a change on one of your GitHub repositories, and a
coding subagent does it on its own while the agent keeps answering you:

> "Start a job on me/shop: cache the catalogue's prices, with tests." → Job a3f1 started,
> about 25 minutes.
>
> (later, in your chat or as a push) 🛠️ Job a3f1 "Price cache" waits for your answer —
> https://github.com/me/shop/pull/12 Done, tests pass. Should the cache also be
> invalidated when a product is updated?
>
> "Tell job a3f1: yes, at every update."

```
you ─► agent ─ start_job ─► coder: Claude Code, git, gh
 ▲       │                      │ clones the repo, codes, tests, pushes, opens the PR
 └ reports (ntfy, a chat) ◄─────┘
```

Off by default: nothing here exists until `JOBS_RUNNER` is set and a channel can carry the
reports. The coder is Claude (Claude Code, or Anthropic's Managed Agents), whatever model
answers you: the agent itself may run on any provider with `claude_code`. Another coding
agent would be one more class with the methods of `jobs.Backend`.

## How it works

- `start_job` hands the request, with its context (repository, branch, git identity, how
  to open the pull request, what "done" means), to a runner, and returns at once. A
  background task follows the runner's events: each time the coder stops (done, a
  question, a failure), its last words reach you under a line the code writes with the
  job's number and status, and the pull request's address once one appears in them.
- **Reports leave the way the agent's own reports do** ([tasks](tasks.md)): in your chat
  when one is set up ([chat.md](chat.md)), where you answer them in the same
  conversation, else as an ntfy push. They are also kept for a client to fetch
  (`GET /v1/tasks/reports`). While a job you just asked for runs, a short line says where
  it stands about each quarter of its expected time.
- You answer through the agent, from any client: `ask_job` queues your message in the
  session, the coder takes it into account and its reply comes back the same way.
  `list_jobs` says where each job stands, with its last report; `stop_job` interrupts a
  running job and closes it. A session left idle costs nothing; it stays open for
  follow-ups until you stop it.
- Jobs are kept in `DATA_DIR/jobs.json`: after a restart, the agent follows every open
  session again, reads its history first and reports only the stops it had not reported.
  A session the runner no longer knows is closed as stopped, with a ⚠️ line, rather than
  polled forever.

Two runners exist (`JOBS_RUNNER`):

### Claude Code on your subscription (`claude_code`)

The `coder` service of [`compose/coder.yml`](../compose/coder.yml) runs Claude Code through
the [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), signed in with your
Claude account, so a job draws on your plan's limits instead of API credit. Each job is one
Claude Code session in its own folder under `/work`, where the repository is cloned first;
a follow-up resumes the session once the current turn is over, and an interrupt ends the
turn. The coder's system prompt is Claude Code's own plus the house rules in
[`agents/coder.md`](../agents/coder.md); the repository's own `CLAUDE.md` applies as in
your own sessions.

- The house rules are yours to edit: they ship generic (follow the repository's own
  conventions, a branch of your own, tests before pushing, no secrets). Rebuild the
  container after a change.
- The coder container gets its own secrets and none of the agent's (no `env_file`): the
  Claude login (`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`, valid a year), the
  GitHub token (`JOBS_GITHUB_TOKEN`, used by git through `gh` and by the GitHub API) and
  the shared `CODER_TOKEN`. No `ANTHROPIC_API_KEY` goes there: it would outrank the login
  and bill the API. The agent reaches it on an internal network; the coder reaches
  GitHub, Claude and the package registries on a network of its own, and nothing else of
  the stack.
- Tools run without prompts (`dontAsk` mode with a fixed list: files, shell, web fetch);
  anything else is denied. `CODER_MAX_BUDGET_USD` stops a runaway turn on Claude Code's
  own cost estimate, notional on a subscription.
- The agent picks the coder's model per job from the request's complexity, without asking
  you: `JOBS_MODEL_SIMPLE` for a change anyone could make in minutes, `JOBS_MODEL_NORMAL`
  for almost everything, `JOBS_MODEL_COMPLEX` only when it is really hard. A job keeps its
  model for its follow-ups; `list_jobs` shows it.
- **The coder's shell can read its own environment, so the GitHub token's scope is the
  boundary**: give it a fine-grained token limited to the repositories you allow.
- The image has git, `gh`, Python and Node. Add what your repositories need to build and
  test to [`coder/Dockerfile`](../coder/Dockerfile).
- Claude Code's transcripts live on the `coder_work` volume, so a session resumes after a
  restart; a turn cut by a restart is reported as such.

### Managed Agents (`managed`)

A [Managed Agents](https://platform.claude.com/docs/en/managed-agents) session: Anthropic
runs the coder's loop and a per-session sandbox, billed to the API. It needs
`LLM_PROVIDER=anthropic` and its key, and no extra container. `agents/coder.md` is the
agent (its front matter names the model), `environments/coder.yaml` its sandbox with
package registries and GitHub as its only network; `scripts/jobs_setup.py` creates them
once, plus a vault holding the GitHub token as `GH_TOKEN`, substituted into the coder's
calls at egress. The repository is mounted through Anthropic's git proxy, so the token
never enters the sandbox. Each session carries a hard spend cap (`JOBS_BUDGET_USD`, at
list prices; at the cap it pauses, reported with 💸) and an outcome: a separate grader
checks the pull request against the rubric and sends the coder back up to
`JOBS_MAX_ITERATIONS` times. A tool call the sandbox pauses for approval is refused (⛔ in
the report), and the Console shows every session live.

## Guardrails

- Only your own message, in a request that read nothing else, starts a job or writes to
  one. From a request that read an email, a page or a repository, `start_job` and
  `ask_job` wait for your go like the other writes; a scheduled task can only propose
  one. So nothing the agent reads can start code running on your repositories.
- What the coder may touch is bounded by the GitHub token. The house rules keep it on a
  branch of its own, never the default branch.
- The coder's reports are others' content for the agent: they come into a conversation
  as data, and `list_jobs` taints the request like reading GitHub does.
- What leaves your server: the request you gave and the repository's content go to
  Anthropic (Claude Code on your plan, or the Managed Agents session), and what the coder
  writes goes to GitHub. Nothing else the agent knows is handed to a job, except by the
  [nightly review](review.md), which says so.

## Setup

The wizard (`scripts/setup.py`) asks about it once a push channel is chosen. By hand:

1. A way for reports to reach you: a chat ([chat.md](chat.md)) or `NTFY_URL`.
2. A GitHub token on the repositories the coder may work on, with **Contents** and
   **Pull requests** read and write (a fine-grained token limits it to a list).
3. For `claude_code`, on a computer with a browser, signed in to your Claude account:
   ```bash
   claude setup-token
   ```
   Then in the server's `.env`:
   ```bash
   COMPOSE_PATH_SEPARATOR=:
   COMPOSE_FILE=docker-compose.yml:compose/coder.yml     # after the files you already have
   JOBS_RUNNER=claude_code
   JOBS_GITHUB_TOKEN=...
   JOBS_GIT_NAME=Your Name
   JOBS_GIT_EMAIL=you@example.com
   CODER_TOKEN=...                # python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   CLAUDE_CODE_OAUTH_TOKEN=...    # the token setup-token printed
   ```
   and `docker compose up -d --build`. `CLAUDE_CODE_OAUTH_TOKEN` is the same setting as for
   [`ENGINE=subscription`](subscription.md): setting it does not change who answers you.
   Without Docker the coder is yours to run (`coder/`, with the variables of
   `compose/coder.yml`), and `CODER_URL` says where it is.
4. For `managed`, from any computer, with the same Anthropic API key as the server:
   ```bash
   pip install anthropic pyyaml
   ANTHROPIC_API_KEY=... python scripts/jobs_setup.py --github-token ...
   ```
   It writes `jobs.env` with `JOBS_AGENT_ID`, `JOBS_ENVIRONMENT_ID`, `JOBS_VAULT_ID` and the
   token: add them and `JOBS_RUNNER=managed` to the server's `.env`, delete the file,
   restart. To change the agent later, edit `agents/coder.md` and run the script again with
   `--agent-id`, `--environment-id` and `--vault-id`.
5. The startup log says `coding jobs: on, run by claude_code` (or `managed`), or why not.

| Setting | Default | What it does |
|---|---|---|
| `JOBS_RUNNER` | `off` | `claude_code`, `managed` or `off`. |
| `CODER_URL`, `CODER_TOKEN` | set by `compose/coder.yml`, unset | Where the coder container is, and the secret both share. |
| `JOBS_GITHUB_TOKEN` | unset | The coder's GitHub token: its scope is what a job can touch. |
| `JOBS_GIT_NAME`, `JOBS_GIT_EMAIL` | unset | The identity of the coder's commits. |
| `JOBS_MODEL_SIMPLE`, `JOBS_MODEL_NORMAL`, `JOBS_MODEL_COMPLEX` | Claude Sonnet, Opus, the strongest | `claude_code`: the coder's model by complexity. |
| `CODER_MAX_BUDGET_USD` | `15` | `claude_code`: runaway guard per turn. |
| `CODER_RC_NAME`, `CODER_SYNC_REPOS` | `Home agent code`, `false` | Remote Control, below. |
| `JOBS_AGENT_ID`, `JOBS_ENVIRONMENT_ID`, `JOBS_VAULT_ID` | unset | `managed`: written by `scripts/jobs_setup.py`. |
| `JOBS_BUDGET_USD`, `JOBS_MAX_ITERATIONS` | `10`, `5` | `managed`: the spend cap per job and the grader's rounds. |

## Remote Control: the coder as your own Claude Code

Optional, with `claude_code`. Once turned on, the coder runs Claude Code's Remote Control
server in `/work/repos`: open a session from claude.ai/code or the Claude app and it works
on your server, on your subscription. With `CODER_SYNC_REPOS=true` the coder also keeps
every repository the GitHub token reaches cloned there (at start, then fetched every 30
minutes; a clean clone on its default branch is fast-forwarded). Repositories whose name
starts with a dot are not cloned: in the trusted root, a `.claude` folder would hand
Claude Code settings.

Turning it on, once: Remote Control needs a full Claude login, not the setup-token (which
can't open it and would shadow the login, so the Remote Control process drops it). On the
server:

```bash
docker compose exec -it coder sh /app/coder/setup-remote-control.sh
```

It runs `claude auth login` (open the link, sign in, paste the code), then Remote Control
once in the terminal to answer its first questions; stop it with Ctrl-C once it shows its
link, and the coder keeps it running (`/work/remote-control.log`). The login expires:
Claude Code warns days before, and the same script logs in again.

**Permission prompts.** A PermissionRequest hook (`coder/permission_hook.py`, set by
`coder/setup.py` in the coder's Claude settings) posts each prompt of those sessions to
the agent, with the coder's token:

- Through a one-way channel (ntfy), you get a line saying that a prompt waits in the
  Claude app, without its text, once a minute at most. You answer in the Claude app.
- In a chat, the prompt arrives as a write waiting for your go, like any other (a
  choice to tick, or your yes): allowing it is always yours, a written no refuses it. The agent can never
  allow one by itself, whatever it read. No answer within 4 minutes: the hook decides
  nothing and the prompt stays open in the Claude app. A prompt that can't be shown whole
  (over 400 characters, several lines) is left to the app, and three at most wait at a
  time.

What a prompt shows (a command, a path) is written by the coder's model, which may have
read anything: it is shown to you as a question and never acted on by the agent.

## Not included

Compared with the setup this feature comes from, three things are left out: a proxy
publishing the coder's dev servers under a domain (it rested on one particular
reverse-proxy and login setup), a list of every Claude Code session of the coder with
messages relayed to them, and secrets managers inside the coder. The coder's Dockerfile is
the place for what your own projects need.

## Cost

`claude_code`: your Claude plan's usage limits, shared with your own Claude Code sessions;
nothing billed per token. `managed`: the coder's tokens (the model in `agents/coder.md`;
the per-job model choice does not apply) plus the sandbox's running time, on the API,
capped per job by `JOBS_BUDGET_USD`; the Console shows each session's cost.
