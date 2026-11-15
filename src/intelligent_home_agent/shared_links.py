"""Short-lived public links to a picture, so a reverse image search can fetch it.

Google Lens, Yandex, Bing and TinEye search by an image's address; a photo sent in
a chat has none. The agent serves a copy (re-encoded, metadata stripped) at an address
with an unguessable token, for a few minutes, from memory only: a restart forgets it.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

TTL_SECONDS = 15 * 60
MAX_LINKS = 20


@dataclass(frozen=True)
class Shared:
    data: bytes
    mimetype: str
    expires: float


class SharedLinks:
    def __init__(self, clock=time.monotonic) -> None:
        self._links: dict[str, Shared] = {}
        self._clock = clock

    def share(self, data: bytes, mimetype: str, ttl: float = TTL_SECONDS) -> str:
        self._prune()
        while len(self._links) >= MAX_LINKS:
            self._links.pop(next(iter(self._links)))
        token = secrets.token_urlsafe(24)
        self._links[token] = Shared(data, mimetype, self._clock() + ttl)
        return token

    def get(self, token: str) -> Shared | None:
        self._prune()
        return self._links.get(token)

    def _prune(self) -> None:
        now = self._clock()
        for token in [t for t, s in self._links.items() if s.expires <= now]:
            del self._links[token]


LINKS = SharedLinks()
