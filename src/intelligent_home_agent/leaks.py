"""What a request holds must not leave in text the model writes.

A hostile page, email or search result can steer the model. The taint rules keep it from
acting and from opening most addresses, and the plain-address rule bounds what one address
can spell out. This is the last net: before text the model wrote goes out (an address to
open, a search query), it is compared with what the request holds: the memory, the agent's
earlier replies and results, and everything its tools read this time. A number, an email
address, a long token or a run of words found in both doesn't go out. The user's own
messages are theirs to reuse, even what an email or a reply repeats of them, and addresses
pages offered were not written by the model.
An address's site name is not checked either: a domain registered before the request
can't spell out what it holds, while the labels before it, the path and the parameters can.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

MIN_NUMBER_DIGITS = 6
LONG_NUMBER_DIGITS = 9
MIN_TOKEN_CHARS = 12
RUN_WORDS = 6
MIN_WORD_CHARS = 4
_DIGIT_GAP = re.compile(r"(?<=\d)[ .\-+_](?=\d)")
_NUMBER = re.compile(rf"\d{{{MIN_NUMBER_DIGITS},}}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LABEL = re.compile(r"[\w-]+")


def _normalize(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in plain.lower())


def _content_words(words: list[str]) -> list[str]:
    return [w for w in words if len(w) >= MIN_WORD_CHARS and not w.isdigit()]


class Leaks:
    def __init__(self) -> None:
        self._numbers: set[str] = set()
        self._emails: set[str] = set()
        self._tokens: set[str] = set()
        self._runs: set[tuple[str, ...]] = set()
        self._own: Leaks | None = None

    def add(self, text: str) -> None:
        """Remember what this text holds that must not go out."""
        if not text:
            return
        joined = _DIGIT_GAP.sub("", text)
        self._numbers.update(_NUMBER.findall(joined))
        self._emails.update(m.lower() for m in _EMAIL.findall(text))
        words = _normalize(joined).split()
        self._tokens.update(w for w in words if len(w) >= MIN_TOKEN_CHARS)
        content = _content_words(words)
        for i in range(len(content) - RUN_WORDS + 1):
            self._runs.add(tuple(content[i : i + RUN_WORDS]))

    def allow(self, text: str) -> None:
        """The user's own words in this request: what they spell out may go out."""
        if self._own is None:
            self._own = Leaks()
        self._own.add(text)

    def add_messages(self, messages: list[dict[str, Any]]) -> None:
        """Earlier turns: the agent's words and what its tools returned, not the user's."""
        for message in messages:
            content = message.get("content")
            if message.get("role") == "assistant" and isinstance(content, str):
                self.add(content)
            elif isinstance(content, list):
                for block in content:
                    if message.get("role") == "assistant" and block.get("type") == "text":
                        self.add(str(block.get("text") or ""))
                    elif block.get("type") == "tool_result":
                        self.add(_result_text(block.get("content")))

    def found(self, text: str, also_allowed: str = "") -> str:
        """What of the request this outgoing text repeats, named in a few words, or "".
        ``also_allowed``: text it may repeat besides the user's own words (the source a
        write cites)."""
        own = self._own or Leaks()
        if also_allowed:
            merged = Leaks()
            merged.add(also_allowed)
            for name in ("_numbers", "_emails", "_tokens", "_runs"):
                getattr(merged, name).update(getattr(own, name))
            own = merged
        raw = unquote(text)
        joined = _DIGIT_GAP.sub("", raw)
        for number in _NUMBER.findall(joined):
            if any(number in known or known in number for known in self._numbers) and not any(
                number in said for said in own._numbers
            ):
                return f"the number {number}"
        # Digits split by anything else ("/3000/6000/0112"): nine in a row from a long
        # known number (a bank account, a phone, a company number) give it away; dates stay shorter.
        digits = "".join(c for c in raw if c.isdigit())
        long_known = [k for k in self._numbers if len(k) >= LONG_NUMBER_DIGITS]
        for i in range(len(digits) - LONG_NUMBER_DIGITS + 1):
            window = digits[i : i + LONG_NUMBER_DIGITS]
            if any(window in known for known in long_known) and not any(
                window in said for said in own._numbers
            ):
                return f"the digits {window}"
        for email in _EMAIL.findall(raw):
            if email.lower() in self._emails and email.lower() not in own._emails:
                return f"the address {email}"
        words = _normalize(joined).split()
        for word in words:
            if len(word) >= MIN_TOKEN_CHARS and word in self._tokens and word not in own._tokens:
                return f"«{word}»"
        content = _content_words(words)
        for i in range(len(content) - RUN_WORDS + 1):
            run = tuple(content[i : i + RUN_WORDS])
            if run in self._runs and run not in own._runs:
                return f"the words «{' '.join(run)}»"
        return ""

    def found_in_address(self, address: str) -> str:
        """Like found, for an address to open, its site's own name left out."""
        return self.found(_without_site(address))


def _without_site(address: str) -> str:
    """The address without the last two labels of its host (the site's name and its
    ending), or unchanged when its host is not a plain domain name."""
    address = unquote(address)
    try:
        parts = urlsplit(address)
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return address
    host = (parts.hostname or "").rstrip(".")
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels[-2:]):
        return address
    try:
        ipaddress.ip_address(host)
        return address
    except ValueError:
        pass
    userinfo, at, _ = parts.netloc.rpartition("@")
    netloc = f"{userinfo}{at}{'.'.join(labels[:-2])}{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""
