#!/usr/bin/env python3
"""Connect the agent to an MCP server that asks for an OAuth login (docs/connectors.md).

    python scripts/mcp_login.py bank https://mcp.example.com/mcp --label "My Bank"

Run it once on a computer with a browser. It finds the server's login endpoints, registers
the agent as a client, opens the login page, and prints the entry to add to MCP_SERVERS in
the server's .env. Standard library only. The entry holds a refresh token: treat it as a
password. Revoke the access any time from the service's own "connected apps" page.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from typing import Any

Fetch = Callable[[str, dict[str, Any] | None, bool], dict[str, Any]]


def fetch(url: str, data: dict[str, Any] | None = None, form: bool = False) -> dict[str, Any]:
    """GET, or POST ``data`` as JSON (or as a form). Raises OSError when it fails."""
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise OSError(f"refusing a login endpoint that is not HTTPS: {url}")
    body, headers = None, {"Accept": "application/json", "User-Agent": "intelligent-home-agent"}
    if data is not None and form:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def discover(mcp_url: str, get: Fetch = fetch) -> dict[str, Any]:
    """The server's OAuth endpoints: its own metadata first, the usual paths otherwise."""
    origin = _origin(mcp_url)
    issuer, resource = origin, origin
    try:
        protected = get(f"{origin}/.well-known/oauth-protected-resource", None, False)
        issuer = (protected.get("authorization_servers") or [origin])[0].rstrip("/")
        resource = protected.get("resource") or origin
    except (OSError, ValueError):
        pass
    meta: dict[str, Any] = {}
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            meta = get(issuer + path, None, False)
            break
        except (OSError, ValueError):
            continue
    return {
        "authorize": meta.get("authorization_endpoint") or f"{issuer}/authorize",
        "token": meta.get("token_endpoint") or f"{issuer}/token",
        "register": meta.get("registration_endpoint") or f"{issuer}/register",
        "resource": resource,
        "scopes": " ".join(meta.get("scopes_supported") or []),
    }


def entry(name: str, label: str, mcp_url: str, endpoints: dict[str, Any], client_id: str,
          refresh_token: str, scope: str) -> dict[str, Any]:  # fmt: skip
    out = {
        "name": name,
        "label": label or name,
        "url": mcp_url,
        "token_url": endpoints["token"],
        "client_id": client_id,
        "refresh_token": refresh_token,
        "resource": endpoints["resource"],
        "writes": "off",
    }
    if scope:
        out["scope"] = scope
    return out


def wait_for_code(port: int, state: str, url: str) -> str:
    received: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Connected. You can close this tab.")
            threading.Thread(target=server.shutdown).start()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Callback)
    print("Opening the login page. If nothing opens, visit:\n" + url, flush=True)
    webbrowser.open(url)
    server.serve_forever()
    if received.get("state") != state or "code" not in received:
        sys.exit(f"Login failed: {received.get('error_description') or received.get('error')}")
    return received["code"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="short name for the agent's tools, e.g. bank")
    parser.add_argument("url", help="the MCP server's address")
    parser.add_argument("--label", default="", help="what you call the service")
    parser.add_argument("--scope", help="scopes to ask for (default: all the server lists)")
    parser.add_argument("--client-id", help="skip registration: a client you already have")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    try:
        endpoints = discover(args.url)
        scope = args.scope if args.scope is not None else endpoints["scopes"]
        client_id = args.client_id
        if not client_id:
            registration = {
                "client_name": "intelligent-home-agent",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
            if scope:
                registration["scope"] = scope
            client_id = fetch(endpoints["register"], registration)["client_id"]
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(
            f"Could not prepare the login ({exc}). If the service gives you a client id, "
            "pass it with --client-id; if it gives a token, put it in MCP_SERVERS as "
            '"token" instead: no login is needed.'
        )

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge.decode().rstrip("="),
        "code_challenge_method": "S256",
        "state": state,
        "resource": endpoints["resource"],
    }
    if scope:
        query["scope"] = scope
    code = wait_for_code(
        args.port, state, endpoints["authorize"] + "?" + urllib.parse.urlencode(query)
    )
    try:
        token = fetch(
            endpoints["token"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "resource": endpoints["resource"],
            },
            True,
        )
    except OSError as exc:
        sys.exit(f"The service refused to finish the login ({exc}).")
    if not token.get("refresh_token"):
        sys.exit(
            "The service gave no refresh token, so the agent could not stay connected. "
            "Ask for the scope that allows it (often offline_access) with --scope."
        )
    made = entry(
        args.name, args.label, args.url, endpoints, client_id, token["refresh_token"], scope
    )
    print("\nConnected. Add this entry to the MCP_SERVERS list in the server's .env")
    print("(MCP_SERVERS=[{...},{...}], on one line), then restart the agent:\n")
    print(json.dumps(made, ensure_ascii=False))
    print('\nIt is read-only; set "writes": "confirm" to let the agent propose changes.')


if __name__ == "__main__":
    main()
