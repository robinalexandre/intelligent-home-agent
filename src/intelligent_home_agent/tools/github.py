"""Pull requests waiting for the user on GitHub, across every repository they can see."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

API_URL = "https://api.github.com"
MAX_PER_GROUP = 10

QUERIES = {
    "review_requested": "is:pr is:open archived:false review-requested:@me",
    "changes_requested_on_mine": "is:pr is:open archived:false author:@me review:changes_requested",
    "approved_mine": "is:pr is:open archived:false author:@me review:approved",
}


class GitHub:
    def __init__(self, http: httpx.AsyncClient, token: str, api_url: str = API_URL) -> None:
        self._http = http
        self._token = token
        self._api_url = api_url.rstrip("/")

    async def search_pull_requests(self, query: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(
                f"{self._api_url}/search/issues",
                params={"q": query, "sort": "updated", "order": "desc", "per_page": MAX_PER_GROUP},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"GitHub unavailable: {exc}") from exc
        return resp.json()


def _summary(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    return {
        "repo": item["repository_url"].split("/repos/", 1)[-1],
        "title": item["title"],
        "author": (item.get("user") or {}).get("login"),
        "draft": item.get("draft", False),
        "open_for_days": (now - created).days,
    }


def build_github_tools(github: GitHub | None) -> list[Tool]:
    if github is None:
        return []

    async def get_github_pull_requests(_args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        results = await asyncio.gather(*(github.search_pull_requests(q) for q in QUERIES.values()))
        now = datetime.now(UTC)
        return {
            group: {
                "count": data.get("total_count", 0),
                "pull_requests": [_summary(item, now) for item in data.get("items", [])],
            }
            for group, data in zip(QUERIES, results, strict=True)
        }

    return [
        Tool(
            name="get_github_pull_requests",
            description=(
                "Open GitHub pull requests waiting for the user, in all their repositories: "
                "reviews requested from them, their own PRs with changes requested, and their "
                "own approved PRs ready to merge."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_github_pull_requests,
            taints=True,
        )
    ]
