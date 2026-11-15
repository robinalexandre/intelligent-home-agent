"""What opens the home asks for proof only the user has, either of:

- the 6 digits their authenticator app shows (TOTP, RFC 6238), from a secret only the server
  and their phone hold;
- a few letters of their secret phrase at positions the code draws at random (as some banks
  do), for when the phone's app is not at hand. Each answer shows a few letters in the chat
  history, so the phrase is long and changed now and then.

The model can't make either up and a page can't supply one, so an injected "open the door"
goes nowhere. A TOTP code works once, whatever it opened; a challenge is drawn by the code,
expires after CHALLENGE_SECONDS and is spent by any answer, right or wrong; MAX_WRONG_CODES
wrong answers in a row block opening for LOCKOUT_SECONDS. Set up with
scripts/unlock_codes_setup.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from intelligent_home_agent.tools.base import ToolError

STEP_SECONDS = 30
DIGITS = 6
MAX_WRONG_CODES = 5
LOCKOUT_SECONDS = 15 * 60
CHALLENGE_LETTERS = 3
CHALLENGE_SECONDS = 5 * 60
MIN_PHRASE_LETTERS = 12
CODE_SCHEMA = {
    "type": "string",
    "description": "The user's answer, as they wrote it in their message: the 6 digits their "
    "authenticator app shows now, or the letters of their secret phrase the last challenge "
    "asked for. Leave it out to get the question to ask them. Never guessed, reused or kept.",
}


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    query = urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS})
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?{query}"


def totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret.upper().replace(" ", "") + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def letters(text: str) -> str:
    """Letters and digits only, lower case, accents dropped: what positions count in."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if ch.isalnum() and not unicodedata.combining(ch))


def write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class UnlockCodes:
    def __init__(
        self,
        store: Path,
        totp_secret: str | None = None,
        phrase: str | None = None,
        clock: Callable[[], float] = time.time,
        draw: Callable[[int, int], list[int]] | None = None,
    ) -> None:
        self._store = store
        self._totp = totp_secret or None
        self._phrase = letters(phrase or "")
        if self._phrase and len(self._phrase) < MIN_PHRASE_LETTERS:
            raise ValueError(f"The secret phrase needs at least {MIN_PHRASE_LETTERS} letters.")
        if not (self._totp or self._phrase):
            raise ValueError("Unlock codes need a TOTP secret or a secret phrase.")
        self._clock = clock
        self._draw = draw or (lambda n, k: secrets.SystemRandom().sample(range(1, n + 1), k))

    def ask(self) -> str:
        """The question for the user. With a phrase, the pending challenge is asked again until
        it is answered or expires: drawing anew on each ask would let someone re-roll until
        the positions fall on letters already seen in the chat history."""
        state = self._load()
        self._blocked(state)
        ways = []
        if self._totp:
            ways.append("the 6-digit code their authenticator app shows now")
        if self._phrase:
            challenge = state.get("challenge")
            if not challenge or self._clock() >= challenge["expires"]:
                challenge = {
                    "positions": sorted(self._draw(len(self._phrase), CHALLENGE_LETTERS)),
                    "expires": self._clock() + CHALLENGE_SECONDS,
                }
                state["challenge"] = challenge
                self._save(state)
            positions = challenge["positions"]
            minutes = max(1, round((challenge["expires"] - self._clock()) / 60))
            listed = ", ".join(map(str, positions[:-1])) + f" and {positions[-1]}"
            ways.append(
                f"letters {listed} of their secret phrase (counting letters only, no spaces "
                f"or accents; this question expires in {minutes} min)"
            )
        return "Ask the user for " + ", or ".join(ways) + ", then call again with `code`."

    def check(self, answer: str) -> None:
        """Return when the answer is right and unspent; otherwise raise, saying why."""
        state = self._load()
        self._blocked(state)
        now = self._clock()
        challenge = state.pop("challenge", None)
        digits = "".join(ch for ch in answer if ch.isdigit())
        if self._totp and len(digits) == DIGITS and digits == "".join(answer.split()):
            step = int(now // STEP_SECONDS)
            for candidate in (step - 1, step, step + 1):
                if candidate > state.get("last_step", -1) and hmac.compare_digest(
                    digits, totp(self._totp, candidate)
                ):
                    self._save({"last_step": candidate})
                    return
        elif self._phrase:
            if not challenge or now >= challenge["expires"]:
                self._save(state)
                raise ToolError(
                    "No question is pending or it expired: call without `code` for a new one."
                )
            wanted = "".join(self._phrase[p - 1] for p in challenge["positions"])
            if hmac.compare_digest(letters(answer), wanted):
                self._save({k: v for k, v in state.items() if k == "last_step"})
                return
        state["wrong"] = state.get("wrong", 0) + 1
        if state["wrong"] >= MAX_WRONG_CODES:
            state.update(wrong=0, locked_until=now + LOCKOUT_SECONDS)
            self._save(state)
            raise ToolError(
                f"Wrong answer, {MAX_WRONG_CODES} in a row: opening is blocked for "
                f"{LOCKOUT_SECONDS // 60} min."
            )
        self._save(state)
        raise ToolError(
            "Wrong, expired or already used answer: call without `code` for a new question, "
            "and ask the user again."
        )

    def _blocked(self, state: dict[str, Any]) -> None:
        now = self._clock()
        if now < state.get("locked_until", 0):
            minutes = int((state["locked_until"] - now) // 60) + 1
            raise ToolError(f"Too many wrong answers: opening is blocked for {minutes} more min.")

    def _load(self) -> dict[str, Any]:
        return json.loads(self._store.read_text()) if self._store.exists() else {}

    def _save(self, state: dict[str, Any]) -> None:
        write_private(self._store, state)
