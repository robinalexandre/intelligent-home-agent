"""Give the agent ADB on the Android TV device, so a helper can see its screen.

Nothing in the agent uses ADB yet (tools/adb.py is the seam for an in-app navigation
helper); this script is here for whoever builds on it. It runs where the agent runs, with
its settings (ANDROID_TV_HOST, HOME_PROXY_URL, DATA_DIR) and the `adb` binary installed.

On the device: Settings > System > About, press Build 7 times; then Developer options >
Wireless debugging on. Then:

    python scripts/android_tv_adb.py pair <pairing port> <code>
        ("Pair device with pairing code" shows both; the key stays under DATA_DIR/android)
    python scripts/android_tv_adb.py tcpip <port>
        (the port under "IP address & Port"): switches ADB to the fixed port 5555
    python scripts/android_tv_adb.py check
        (connects on 5555 and prints the app in front)

In Docker, where the image holds no scripts, send it in on stdin:
`docker compose exec -T agent python - check < scripts/android_tv_adb.py`.

Android closes port 5555 when the device restarts (standby is not a restart): run tcpip
again with the new port then. The pairing stays.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from intelligent_home_agent.config import get_settings
from intelligent_home_agent.tools.adb import Adb
from intelligent_home_agent.tools.android_tv import Tunnel


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    proxy = settings.home_proxy_url or settings.ha_proxy_url
    key_dir = str(Path(settings.data_dir) / "android")
    adb = Adb(settings.android_tv_host or "", key_dir, proxy)
    if args.action == "check":
        async with adb.session() as session:
            print("ADB works; in front:", await session.foreground_app())
        return
    if args.action == "tcpip":
        async with adb.session(port=args.port) as session:
            print((await adb.run("-s", session.serial, "tcpip", "5555")).decode().strip())
        return
    host, port = settings.android_tv_host or "", args.port
    if proxy:
        async with Tunnel(proxy, host, port, 10) as local:
            print((await adb.run("pair", f"127.0.0.1:{local}", args.code)).decode().strip())
    else:
        print((await adb.run("pair", f"{host}:{port}", args.code)).decode().strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["pair", "tcpip", "check"])
    parser.add_argument("port", type=int, nargs="?", default=0)
    parser.add_argument("code", nargs="?", default="")
    asyncio.run(main(parser.parse_args()))
