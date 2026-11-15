"""The Apple TV and HomePods for the agent (tools/apple_media.py), run on the home network.

    pip install -e ".[apple]"
    python scripts/apple_devices.py profile 192.168.1.61 192.168.1.62
        prints the value of APPLE_DEVICES: the mDNS properties pyatv needs, as an agent
        outside the home can't scan. Give each device's address.
    python scripts/apple_devices.py pair 192.168.1.61 Companion
        pairs with the Apple TV (a PIN shows on the TV; type it at the prompt) and prints
        the value of APPLE_TV_COMPANION_CREDENTIALS; the same with AirPlay for
        APPLE_TV_AIRPLAY_CREDENTIALS. Treat both as passwords.

HomePods need no pairing. On macOS, the terminal needs Local Network access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import pyatv
from pyatv.const import Protocol

# AirPlay properties pyatv reads to set up a device it did not discover.
AIRPLAY_KEYS = {"model", "osvers", "features", "flags", "acl", "pw", "act", "deviceid", "pi", "psi"}


async def profile(hosts: list[str]) -> None:
    loop = asyncio.get_running_loop()
    devices = []
    for conf in await pyatv.scan(loop, hosts=hosts, timeout=5):
        services = {}
        for service in conf.services:
            if service.protocol not in (Protocol.AirPlay, Protocol.Companion):
                continue
            props = dict(service.properties)
            if service.protocol == Protocol.AirPlay:
                props = {k: v for k, v in props.items() if k in AIRPLAY_KEYS}
            services[service.protocol.name] = {"port": service.port, "properties": props}
        devices.append(
            {
                "name": conf.name,
                "host": str(conf.address),
                "id": conf.identifier,
                "services": services,
            }
        )
    print(json.dumps(devices, separators=(",", ":"), ensure_ascii=False))


async def pair(host: str, protocol_name: str) -> None:
    loop = asyncio.get_running_loop()
    found = await pyatv.scan(loop, hosts=[host], timeout=5)
    if not found:
        sys.exit(f"Nothing answers at {host}.")
    pairing = await pyatv.pair(found[0], Protocol[protocol_name], loop, name="Home agent")
    try:
        await pairing.begin()
        print("PIN shown on the TV: ", end="", file=sys.stderr, flush=True)
        pin = await loop.run_in_executor(None, sys.stdin.readline)
        pairing.pin(int(pin.strip()))
        await pairing.finish()
        if not pairing.has_paired:
            sys.exit("Not paired.")
        print(pairing.service.credentials)
    finally:
        await pairing.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    profile_args = commands.add_parser("profile")
    profile_args.add_argument("hosts", nargs="+")
    pair_args = commands.add_parser("pair")
    pair_args.add_argument("host")
    pair_args.add_argument("protocol", choices=["Companion", "AirPlay"])
    args = parser.parse_args()
    if args.command == "profile":
        asyncio.run(profile(args.hosts))
    else:
        asyncio.run(pair(args.host, args.protocol))


if __name__ == "__main__":
    main()
