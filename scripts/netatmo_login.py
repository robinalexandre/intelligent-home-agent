"""Connect the agent to your Netatmo account once, from a computer with a browser.

    python scripts/netatmo_login.py

First create an app on https://dev.netatmo.com (My apps > Create) with the redirect URI
this script prints, and keep its client id and secret at hand. The script opens Netatmo's
login page (sign in, accept the access: cameras read and on/off, weather station and
thermostat read), checks that the homes can be read, then writes NETATMO_CLIENT_ID,
NETATMO_CLIENT_SECRET and NETATMO_REFRESH_TOKEN to netatmo-credentials.env (chmod 600).
Add those three lines to the .env the agent reads, restart the agent, then delete the
file. Netatmo may rotate the refresh token on each use: the one written is the live one,
so don't run the agent with an older one in between. Revoke the access any time on
https://auth.netatmo.com (Account > Third-party apps).
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
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from intelligent_home_agent.tools.netatmo import (  # noqa: E402
    AUTHORIZE_URL,
    CAMERA_TYPES,
    ENERGY_TYPES,
    SCOPES,
    TOKEN_URL,
    WEATHER_TYPES,
    Netatmo,
    NetatmoAuth,
)


async def check(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Read the homes as the agent will, and return the live token."""
    with tempfile.TemporaryDirectory() as tmp:
        async with httpx.AsyncClient(timeout=30) as client:
            auth = NetatmoAuth(client, client_id, client_secret, refresh_token, Path(tmp, "n"))
            homes = await Netatmo(client, auth).homes()
    for home in homes:
        types = [m.get("type") for m in home.get("modules") or []]
        cameras = sum(t in CAMERA_TYPES for t in types)
        extras = [
            label
            for label, kinds in (("weather station", WEATHER_TYPES), ("heating", ENERGY_TYPES))
            if any(t in kinds for t in types)
        ]
        persons = sum(1 for p in home.get("persons") or [] if p.get("pseudo"))
        print(
            f"{home.get('name')}: {cameras} camera(s), {persons} known person(s)"
            + (f", {', '.join(extras)}" if extras else "")
        )
    if not homes:
        print("Connected, but no home on this account.")
    return auth.refresh_token or refresh_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8586)
    parser.add_argument("--out", type=Path, default=Path("netatmo-credentials.env"))
    args = parser.parse_args()
    redirect_uri = f"http://localhost:{args.port}"
    print(f"Redirect URI to register in the Netatmo app: {redirect_uri}")
    client_id = os.environ.get("NETATMO_CLIENT_ID") or input("Client id: ").strip()
    client_secret = os.environ.get("NETATMO_CLIENT_SECRET") or getpass.getpass("Secret: ")

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
            self.wfile.write(b"Netatmo is connected, you can close this tab.")
            threading.Thread(target=server.shutdown).start()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("localhost", args.port), Callback)
    url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )
    print("Opening Netatmo's login page. If nothing opens, visit:\n" + url, flush=True)
    webbrowser.open(url)
    server.serve_forever()

    if received.get("state") != state or "code" not in received:
        sys.exit(f"Login failed: {received.get('error_description') or received.get('error')}")
    token = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": received["code"],
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        },
    )
    refresh_token = token.json().get("refresh_token") if token.status_code == 200 else None
    if not refresh_token:
        sys.exit(f"Netatmo refused the code (HTTP {token.status_code}).")

    refresh_token = asyncio.run(check(client_id, client_secret, refresh_token))
    args.out.write_text(
        f"NETATMO_CLIENT_ID={client_id}\n"
        f"NETATMO_CLIENT_SECRET={client_secret}\n"
        f"NETATMO_REFRESH_TOKEN={refresh_token}\n"
    )
    os.chmod(args.out, 0o600)
    print(
        f"Connected. Credentials written to {args.out} (not shown here).\n"
        "Add those three lines to the .env the agent reads, restart the agent,\n"
        f"then delete {args.out}."
    )


if __name__ == "__main__":
    main()
