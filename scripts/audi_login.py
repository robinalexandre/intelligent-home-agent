"""Connect the agent to your myAudi account once, from a computer with a browser.

    python scripts/audi_login.py --country GB

This uses the private API of the myAudi app, which Audi does not publish or support: it
may change or stop without notice, and Audi's terms may not allow it. Read them first
(docs/home-devices.md).

Audi signs accounts in through its web login, two-factor included. The script opens it,
catches the answer on http://localhost:8080 (the address the myAudi client is registered
with, so the port is fixed), reads the car once as the agent will, then writes
CAR_PROVIDER, CAR_REFRESH_TOKEN, CAR_CLIENT_ID and CAR_VIN to car-credentials.env
(chmod 600) without showing the token. Audi rotates the refresh token on each use: the one
written is the live one, so don't run anything else with it before the agent restarts.
The S-PIN (CAR_PIN) is set apart, by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
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
from intelligent_home_agent.tools.audi import (  # noqa: E402
    AUDIENCE,
    AUTHORIZE_URL,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
    USER_AGENT,
    Audi,
    AudiAuth,
    discover_client_id,
    list_vehicles,
)
from intelligent_home_agent.tools.base import ToolError  # noqa: E402


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def wait_for_code(url: str) -> dict[str, str]:
    received: dict[str, str] = {}
    redirect = urllib.parse.urlparse(REDIRECT_URI)

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != redirect.path or not ({"code", "error"} & query.keys()):
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"myAudi is connected, you can close this tab.")
            threading.Thread(target=server.shutdown).start()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer((redirect.hostname, redirect.port), Callback)
    print("Opening the myAudi login page. If nothing opens, visit:\n" + url, flush=True)
    webbrowser.open(url)
    server.serve_forever()
    return received


async def connect(country: str, wanted_vin: str | None, out: Path) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        client_id, language = await discover_client_id(client, country)
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(16)
        url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "audience": AUDIENCE,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "ui_locales": f"{language}-{country.upper()}",
            }
        )
        received = await asyncio.to_thread(wait_for_code, url)
        if received.get("state") != state or "code" not in received:
            sys.exit(f"Login failed: {received.get('error_description') or received.get('error')}")
        token = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": received["code"],
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        body = token.json() if token.status_code == 200 else {}
        if not body.get("refresh_token"):
            sys.exit(f"myAudi refused the code (HTTP {token.status_code}).")

        vehicles = await list_vehicles(client, body["access_token"])
        for car in vehicles:
            model = ((car.get("vehicle") or {}).get("media") or {}).get("shortName") or "?"
            print(f"Car: {car.get('nickname') or model} ({model}), VIN …{car['vin'][-4:]}")
        vin = wanted_vin or (vehicles[0]["vin"] if len(vehicles) == 1 else None)
        if not vin:
            vin = (
                await asyncio.to_thread(input, "VIN of the car (on its registration papers): ")
            ).strip()

        # Written at once and again after the check: a login is never lost to a failed read,
        # and the file always holds the token Audi rotated last.
        values = {
            "CAR_PROVIDER": "audi",
            "CAR_REFRESH_TOKEN": body["refresh_token"],
            "CAR_CLIENT_ID": client_id,
            "CAR_VIN": vin,
        }
        save(out, values)
        with tempfile.TemporaryDirectory() as tmp:
            auth = AudiAuth(client, client_id, body["refresh_token"], Path(tmp, "car.json"))
            try:
                seen = await Audi(client, auth, vin).state()
                print(
                    "Read from the car: "
                    + ", ".join(
                        f"{k}={seen[k]}"
                        for k in ("locked", "range_km", "fuel_percent")
                        if k in seen
                    )
                    + (f"; services: {', '.join(seen['services'])}" if seen.get("services") else "")
                )
            except ToolError as exc:
                print(f"Connected, but reading the car failed: {exc}")
            finally:
                values["CAR_REFRESH_TOKEN"] = auth.refresh_token or body["refresh_token"]
                save(out, values)


def save(out: Path, values: dict[str, str]) -> None:
    out.touch(mode=0o600)
    os.chmod(out, 0o600)
    out.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--country",
        required=True,
        help="The country of your myAudi account, as two letters: GB, DE, FR...",
    )
    parser.add_argument("--vin", help="The car's VIN, when the account has several.")
    parser.add_argument("--out", type=Path, default=Path("car-credentials.env"))
    args = parser.parse_args()
    asyncio.run(connect(args.country, args.vin, args.out))
    print(
        f"Credentials written to {args.out} (not shown here).\n"
        "Add its lines to the agent's .env, with, for locking and the heating, the myAudi\n"
        "S-PIN as CAR_PIN, and CAR_NAME if you call the car something. Then restart the\n"
        f"agent (docker compose up -d) and delete {args.out}."
    )


if __name__ == "__main__":
    main()
