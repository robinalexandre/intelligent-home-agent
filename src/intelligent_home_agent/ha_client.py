"""Thin async client for the Home Assistant REST API."""

from __future__ import annotations

from typing import Any

import httpx


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            proxy=proxy,
            transport=transport,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ping(self) -> bool:
        try:
            resp = await self._http.get("/api/")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def get_states(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/states")

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/states/{entity_id}")

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        return_response: bool = False,
    ) -> Any:
        params = {"return_response": "true"} if return_response else None
        return await self._request(
            "POST", f"/api/services/{domain}/{service}", json=data, params=params
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"Home Assistant unreachable: {exc}") from exc
        if resp.status_code == 404:
            raise HomeAssistantError(f"Not found: {path}")
        if resp.status_code >= 400:
            raise HomeAssistantError(f"Home Assistant error {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else None
