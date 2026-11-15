# Connectors: your bank, your hosting provider, any service with an MCP server

| | |
|---|---|
| **What it does** | Plugs in any service that has an MCP server (a bank, an accounting tool, a cloud provider): read freely, changed only on your yes. |
| **What you need** | The server's HTTPS address and a login: an API token, OAuth client credentials, or a browser login done once with `scripts/mcp_login.py`. |
| **Settings** | `MCP_SERVERS` (one line of JSON). The wizard does not ask for it. |
| **Check it works** | `<name>_help` and `<name>_read` are in the startup log's tools; ask a question the service can answer. |
| **Verified** | Not run against the real service yet: the test suite only, against simulated services. |

Many services now publish an [MCP](https://modelcontextprotocol.io) server: banks and
accounting tools, hosting and cloud providers, rental or project management, CRMs. The
agent can use any of them that is reachable over HTTPS, so "what did I spend on hosting
this month?" or "is my server short on disk?" get an answer from the service itself.

Nothing is specific to one vendor: you list the servers in `MCP_SERVERS`, and each one
gets its own tools, named after the short name you give it.

```bash
MCP_SERVERS=[{"name":"bank","label":"My Bank","url":"https://mcp.bank.example/mcp","token":"..."},{"name":"hosting","label":"My Host","url":"https://mcp.host.example/mcp","token":"..."}]
```

One line of JSON, in `.env`. For each entry:

| Key | Meaning |
|---|---|
| `name` | Short name, lowercase: `bank` gives the tools `bank_help`, `bank_read` and, with writes, `bank_write`. |
| `label` | What you call the service; the agent uses it when it talks to you. |
| `url` | The MCP server's address (streamable HTTP), from the service's documentation. |
| `writes` | `off` (default): the agent only reads. `confirm`: it may propose changes, each one waiting for your yes. |
| `read_tools` | Names of server tools that only read, when the server forgot to flag them read-only (they would otherwise count as writes). |
| login keys | One of the three ways below. |

## Logging in

Use what the service offers, in this order of simplicity:

1. **An API token** you create in the service's settings: `"token": "..."`.
2. **OAuth client credentials** (a client id and a secret, for server-to-server access):
   `"client_id"`, `"client_secret"`, `"token_url"`, and `"resource"` or `"scope"` if the
   service asks for them.
3. **An OAuth login in the browser**, when the service only knows that one (most banks).
   Run this once on a computer with a browser:

   ```bash
   python scripts/mcp_login.py bank https://mcp.bank.example/mcp --label "My Bank"
   ```

   It finds the service's login endpoints, registers the agent as a client, opens the
   login page, and prints the entry to paste into `MCP_SERVERS`. `--scope` narrows what
   you grant (e.g. only the read scopes); `--client-id` is for a service that gives you a
   client instead of letting the agent register. If the service renews the login at each
   use, the agent keeps the latest one in its data volume (`agent_data`).

One worked example among others, here a business bank account at Qonto, read-only
(the address and the scope names are that service's; yours are in your service's
documentation):

```bash
python scripts/mcp_login.py bank https://mcp.qonto.com/mcp --label Qonto \
  --scope "offline_access organization.read client.read client_invoices.read supplier_invoice.read"
```

Any other bank, a hosting provider or an accounting tool works the same way with its own
address and scopes. A service whose MCP server only runs on your own computer (started by a command
rather than reached at an address) is not supported yet.

## What the agent may do

The rules are enforced by the code, whatever the model or a message says:

- **Reading taints the request, and so does `<name>_help`.** An account holds text
  other people wrote (a payment's label, a customer's message, a log line), and the
  tool descriptions `<name>_help` returns are written by the remote server. Once the
  agent has called either on a connector, nothing acts on the home, writes or searches the web for the rest of that request
  without your go, and what was read is dropped from memory after the reply.
- **A write never runs when asked.** With `"writes": "confirm"`, the call is parked, as
  every action that waits for you is (see "Security model" in the README); the reply ends
  with a numbered list written by the code, not by the model, that shows the exact call:

  ```
  Waiting for your go ("yes", or "yes except 2"):
  1. My Bank: add_customer {"name": "Acme"}
  ```

  It runs only if your next message agrees ("yes", "yes except 2", "no"), and only if
  that message's request has read nothing else. A tool the service doesn't flag as
  harmless is shown with "(may delete or overwrite)". A call that is not one of the
  service's write tools, or whose arguments are too long or hold hidden characters to be
  shown whole, is refused before you are asked. What waits is forgotten with the
  conversation (`CONVERSATION_TTL_SECONDS`, five minutes by default for voice;
  `CHAT_CONVERSATION_TTL_SECONDS`, twelve hours, with `"display": "chat"`): answer within
  that time, or ask again.
- **With `"writes": "off"`**, the write tools don't exist: there is nothing to talk the
  agent into.
- **What the token can do is the outer limit.** Grant the narrowest scopes the service
  offers. A bank's MCP server typically cannot move money at all, whatever is asked;
  check what yours allows before turning writes on.

A service with a hundred tools doesn't slow every request down: the agent first asks
`<name>_help` what exists, then calls the one tool it needs.
