# Your servers at a hosting provider

| | |
|---|---|
| **What it does** | Reads your servers at a hosting provider (state, metrics, backups, Docker projects and their logs) and, on your yes, restarts, snapshots or deploys. |
| **What you need** | An API token at Hostinger or Hetzner Cloud. Another provider: a [connector](connectors.md) if it has an MCP server. |
| **Settings** | `HOSTING_PROVIDER`, `HOSTING_API_TOKEN`; the wizard's `hosting` item. |
| **Check it works** | `list_servers` is in the startup log's tools; ask "how are my servers?". |
| **Verified** | Run against the real service: Hostinger. Not run: Hetzner, written from its documentation ([below](#which-provider)). |

"Is my server short on disk?", "why is the site down?", "restart the shop", "take a
snapshot before I upgrade": with a provider and an API token, the agent reads your
servers through the provider's public API and, on your yes, acts on them.

```bash
HOSTING_PROVIDER=hostinger     # or: hetzner
HOSTING_API_TOKEN=...
```

Both empty (the default): no tool, and nothing in the prompt. `scripts/setup.py` asks for
them when you pick `hosting` in its menu.

## Which provider

The tools are the same whatever the provider (`list_servers`, `get_server_metrics`,
`restart_server`...); a tool exists only when the provider can serve it.

| | `hostinger` | `hetzner` |
|---|---|---|
| What it is | Hostinger VPS | Hetzner Cloud servers (one project per token) |
| Servers, one in detail (`list_servers`, `get_server`) | yes | yes |
| Metrics over the last hours (`get_server_metrics`) | CPU, memory, disk, network, uptime | CPU, disk, network (Hetzner measures no memory) |
| Recent actions (`list_server_actions`) | yes | yes |
| Backups and snapshots (`list_server_backups`) | yes | yes |
| Firewalls (`list_server_firewalls`) | yes | yes |
| Malware scan (`get_server_malware_scan`) | yes (Monarx) | no |
| Docker projects: list, compose file, containers, logs | yes | no |
| Restart, start, stop, update a Docker project | yes | no |
| Deploy a compose file (`deploy_server_project`) | yes | no |
| Restart the server (`restart_server`) | yes | yes (a soft reboot) |
| Snapshot (`take_server_snapshot`) | yes, it replaces the previous one | yes, each one is kept and billed until you delete it |
| Where the token is made | hPanel > API | Cloud Console > the project > Security > API tokens |
| Tested | run against the real API | **written from Hetzner's documentation, never run against the real API** |

Read the last row: the Hetzner provider's calls are checked by tests against what the
documentation describes, not against Hetzner. Expect to fix a detail the first time you
use it, and please report it.

A Hetzner token is made *Read* or *Read & Write*. With a read-only token the agent can
look and nothing more: Hetzner itself refuses the restart and the snapshot. That is the
safest way to start. A Hostinger token opens the whole account, including domains and
billing: the agent only ever calls the fixed list of VPS paths behind the tools above.

**Another provider?** Two ways:

- If it publishes an MCP server (many hosting and cloud providers do), plug it in as a
  connector, with no code: [connectors.md](connectors.md).
- Otherwise a provider is one class in `tools/hosting.py`: subclass `HostingApi`, write
  the methods it can serve (`list_servers`, `get_server`, `server_name`, `get_metrics`,
  `list_backups`, `restart_server`, `take_snapshot`...), add it to `PROVIDERS`, to the
  `HOSTING_PROVIDER` values in `config.py` and to `HOSTING` in `scripts/setup.py`. The
  tools, their rules and their wording come by themselves.

## What the agent may and may not do

- **Reading is free**, and what it reads (logs, compose files, hostnames) was partly
  written by others: it taints the request like an email. After such a read, nothing
  acts or leaves without your go.
- **An action** (restart, start, stop or update a Docker project, restart the server,
  take a snapshot) runs at once only when you asked for it yourself in a request that
  read nothing; otherwise it waits in the list under the reply, in one line that names
  the provider, the server and the project: `Hostinger: restart “shop” on the server
  srv1.example.com`. It never runs by itself from a scheduled task: there it waits for
  you too.
- **A deployment always waits**, even when you asked for it: you see the whole compose
  file first. Settings that give a container power over the host are flagged with ⚠️:
  `privileged`, the host's network or processes, added capabilities, the Docker socket,
  a folder of the host mounted. The compose file must be the YAML itself; a link to one
  is refused, since what is behind a link can change after you said yes.
- **There is nothing else**: no deleting a server, a backup or a project, no rebuilding,
  no restoring a backup, no firewall change, no billing, no domains. Do those in the
  provider's console.
- If the agent runs on one of these servers, restarting that server or its own project
  cuts it off for a moment; it is told to warn you.

A read that fails on the provider's side is tried once more; an action never is, because
it may have run despite the error.
