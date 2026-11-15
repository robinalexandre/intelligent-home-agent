"""Pair the agent with a Google TV / Android TV device once, from the home network.

    pip install -e ".[android-tv]"
    python scripts/android_tv_pair.py 192.168.1.60

The device shows a code on screen: type it here. The script makes a client certificate,
has the device accept it, checks that it can read the device's state with it, then writes
ANDROID_TV_HOST, ANDROID_TV_CLIENT_CERT and ANDROID_TV_CLIENT_KEY to
android-tv-credentials.env (chmod 600). Add those three lines to the .env the agent reads,
restart the agent, then delete the file. The device lists the agent as "Home agent" among
its paired remotes (Settings > Remotes & accessories, or the Google TV remote app's
settings), where it can be removed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

from androidtvremote2 import AndroidTVRemote
from androidtvremote2.certificate_generator import generate_selfsigned_cert

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from intelligent_home_agent.tools.android_tv import (  # noqa: E402
    CLIENT_NAME,
    AndroidTV,
    _state,
    encode_pem,
)


def write_pair(folder: str, cert_pem: bytes, key_pem: bytes) -> tuple[Path, Path]:
    certfile, keyfile = Path(folder, "cert.pem"), Path(folder, "key.pem")
    certfile.write_bytes(cert_pem)
    keyfile.write_bytes(key_pem)
    return certfile, keyfile


async def pair(host: str) -> tuple[str, str]:
    cert_pem, key_pem = generate_selfsigned_cert(CLIENT_NAME)
    with tempfile.TemporaryDirectory() as tmp:
        certfile, keyfile = write_pair(tmp, cert_pem, key_pem)
        remote = AndroidTVRemote(CLIENT_NAME, str(certfile), str(keyfile), host)
        name, _ = await remote.async_get_name_and_mac()
        print(f"Pairing with {name}: a code shows on its screen.", flush=True)
        await remote.async_start_pairing()
        code = (await asyncio.to_thread(input, "Code: ")).strip()
        await remote.async_finish_pairing(code)
    cert, key = encode_pem(cert_pem), encode_pem(key_pem)
    async with AndroidTV(host, cert, key).connect() as connected:
        state = _state(connected)
    print(f"Paired. {state['device'] or name}: on={state['on']}, app={state['app']}")
    return cert, key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="the device's address on the home network")
    parser.add_argument("--out", type=Path, default=Path("android-tv-credentials.env"))
    args = parser.parse_args()
    cert, key = asyncio.run(pair(args.host))
    args.out.write_text(
        f"ANDROID_TV_HOST={args.host}\nANDROID_TV_CLIENT_CERT={cert}\nANDROID_TV_CLIENT_KEY={key}\n"
    )
    os.chmod(args.out, 0o600)
    print(
        f"Credentials written to {args.out} (not shown here).\n"
        "Add those three lines to the .env the agent reads, restart the agent,\n"
        f"then delete {args.out}."
    )


if __name__ == "__main__":
    main()
