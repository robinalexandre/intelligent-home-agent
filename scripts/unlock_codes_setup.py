"""Set up the proof the agent asks before opening the door (tools/unlock_codes.py).

    pip install "qrcode[pil]" && python scripts/unlock_codes_setup.py --totp --phrase

--totp makes a new authenticator secret and opens its QR code as an image (never printed):
scan it with an authenticator app (Apple Passwords, Google Authenticator, Aegis...), then
type the code it shows to check. --phrase asks for a secret phrase of at least 12 letters,
typed without being shown. Both go to unlock-credentials.env (chmod 600): add its lines to
the .env the agent reads, restart the agent, then delete the file. Run either again to
change it.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from intelligent_home_agent.tools.unlock_codes import (  # noqa: E402
    MIN_PHRASE_LETTERS,
    STEP_SECONDS,
    letters,
    new_secret,
    otpauth_uri,
    totp,
)


def open_image(path: Path) -> None:
    opener = {"darwin": ["open"], "win32": ["cmd", "/c", "start", ""]}.get(
        sys.platform, ["xdg-open"]
    )
    subprocess.run([*opener, str(path)], check=False)  # noqa: S603 - a fixed opener and our own file


def setup_totp(issuer: str) -> str:
    try:
        import qrcode
    except ImportError:
        sys.exit('The QR code needs qrcode: pip install "qrcode[pil]"')
    secret = new_secret()
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp, "unlock-qr.png")
        qrcode.make(otpauth_uri(secret, "door", issuer)).save(image)
        os.chmod(image, 0o600)
        open_image(image)
        for _ in range(3):
            code = input("Scan the QR code, then type the code the app shows: ").strip()
            step = int(time.time() // STEP_SECONDS)
            if code.replace(" ", "") in {totp(secret, s) for s in (step - 1, step, step + 1)}:
                print("The authenticator app is set.")
                time.sleep(1)
                return secret
            print("That code doesn't match, try the next one.")
    sys.exit("No matching code: nothing written. Delete the entry in the app and start again.")


def setup_phrase() -> str:
    while True:
        phrase = getpass.getpass(f"Secret phrase ({MIN_PHRASE_LETTERS}+ letters, not shown): ")
        if set(phrase) & set('"\\$`'):
            print("Keep quotes, backslashes, $ and backticks out of it.")
            continue
        if len(letters(phrase)) < MIN_PHRASE_LETTERS:
            print(f"Too short: {len(letters(phrase))} letters.")
            continue
        if getpass.getpass("Again: ") == phrase:
            print(
                f"{len(letters(phrase))} letters: positions count letters and digits only, "
                "no spaces, accents or punctuation."
            )
            return phrase
        print("The two don't match.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--totp", action="store_true", help="a new authenticator secret")
    parser.add_argument("--phrase", action="store_true", help="a new secret phrase")
    parser.add_argument("--issuer", default="Home agent", help="the name shown in the app")
    parser.add_argument("--out", type=Path, default=Path("unlock-credentials.env"))
    args = parser.parse_args()
    if not (args.totp or args.phrase):
        parser.error("say --totp, --phrase or both")
    values = {}
    if args.totp:
        values["UNLOCK_TOTP_SECRET"] = setup_totp(args.issuer)
    if args.phrase:
        values["UNLOCK_PHRASE"] = setup_phrase()
    args.out.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
    os.chmod(args.out, 0o600)
    print(
        f"Written to {args.out} (not shown here). Add its lines to the .env the agent reads, "
        f"restart the agent, then delete {args.out}."
    )


if __name__ == "__main__":
    main()
