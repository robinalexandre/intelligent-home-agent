"""Connect the agent to your Withings account once, from a computer with a browser.

    python scripts/withings_login.py

First create an app on https://developer.withings.com (Public API integration) with the
callback URL this script prints, and keep its client id and secret at hand. The script
opens Withings' login page (sign in, accept reading your measures), checks that the
weigh-ins can be read, then writes WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET and
WITHINGS_REFRESH_TOKEN to withings-credentials.env (chmod 600). Add those three lines to
the .env the agent reads, restart it and delete the file. Withings rotates the refresh
token on each use:
the one written is the live one, so don't run the agent with an older one in between.
Revoke the access any time in the Withings app (Settings > Partner apps).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import http.server
import os
import secrets
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from intelligent_home_agent.tools.withings import (  # noqa: E402
    AUTHORIZE_URL,
    SCOPES,
    TOKEN_URL,
    Withings,
    WithingsAuth,
)


async def check(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Read the last month's weigh-ins as the agent will, and return the live token."""
    with tempfile.TemporaryDirectory() as tmp:
        async with httpx.AsyncClient(timeout=30) as client:
            auth = WithingsAuth(client, client_id, client_secret, refresh_token, Path(tmp, "w"))
            now = datetime.now().astimezone()
            found = await Withings(client, auth).weigh_ins(now - timedelta(days=30), now)
    if found:
        latest = found[-1]
        print(f"{len(found)} weigh-ins in 30 days, the latest on {latest.at:%Y-%m-%d}:")
        print(latest.values)
    else:
        print("Connected, but no weigh-in in the last 30 days.")
    return auth.refresh_token or refresh_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8585)
    parser.add_argument("--out", type=Path, default=Path("withings-credentials.env"))
    args = parser.parse_args()
    redirect_uri = f"http://localhost:{args.port}"
    print(f"Callback URL to register in the Withings app: {redirect_uri}")
    client_id = os.environ.get("WITHINGS_CLIENT_ID") or input("Client id: ").strip()
    client_secret = os.environ.get("WITHINGS_CLIENT_SECRET") or getpass.getpass("Secret: ")

    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not ({"code", "error"} & query.keys()):
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Withings is connected, you can close this tab.")
            threading.Thread(target=server.shutdown).start()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("localhost", args.port), Callback)
    url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    print("Opening Withings' login page. If nothing opens, visit:\n" + url, flush=True)
    webbrowser.open(url)
    server.serve_forever()

    if received.get("state") != state or "code" not in received:
        sys.exit(f"Login failed: {received.get('error_description') or received.get('error')}")
    # The code is only valid for 30 seconds.
    token = httpx.post(
        TOKEN_URL,
        data={
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": received["code"],
            "redirect_uri": redirect_uri,
        },
    ).json()
    refresh_token = (token.get("body") or {}).get("refresh_token")
    if token.get("status") != 0 or not refresh_token:
        sys.exit(f"Withings refused the code (status {token.get('status')}).")

    refresh_token = asyncio.run(check(client_id, client_secret, refresh_token))
    args.out.write_text(
        f"WITHINGS_CLIENT_ID={client_id}\n"
        f"WITHINGS_CLIENT_SECRET={client_secret}\n"
        f"WITHINGS_REFRESH_TOKEN={refresh_token}\n"
    )
    os.chmod(args.out, 0o600)
    print(
        f"Connected. Credentials written to {args.out}: add them to the .env the agent reads, "
        "restart it, then delete the file."
    )


if __name__ == "__main__":
    main()
