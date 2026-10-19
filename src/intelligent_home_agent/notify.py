"""Push notifications through ntfy (https://ntfy.sh or a self-hosted server), optional.

The ntfy app on the phone (Android, iOS) or the web app on the desktop subscribes to the
topic. Anyone who knows a topic on the public server can read it: use a long random topic
name, or a self-hosted server with an access token.
"""

from __future__ import annotations

import httpx


class NotifyError(RuntimeError):
    pass


class Ntfy:
    def __init__(self, http: httpx.AsyncClient, topic_url: str, token: str | None = None) -> None:
        self._http = http
        self._url = topic_url
        self._token = token

    async def send(self, title: str, message: str) -> None:
        # The title goes in the query string: ntfy reads it there, and it may hold non-ASCII.
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = await self._http.post(
                self._url, params={"title": title}, content=message.encode(), headers=headers
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy unavailable: {type(exc).__name__}") from exc
