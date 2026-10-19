from datetime import UTC, datetime, timedelta

import httpx
import respx

from intelligent_home_agent.tools import GitHub, build_github_tools
from intelligent_home_agent.tools.github import API_URL, QUERIES


def item(repo, title, days_open):
    created = datetime.now(UTC) - timedelta(days=days_open, hours=1)
    return {
        "repository_url": f"{API_URL}/repos/{repo}",
        "title": title,
        "user": {"login": "alex"},
        "draft": False,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def search_response(request):
    if "review-requested:@me" in request.url.params["q"]:
        items = [item("acme/api", "Add invoices", 3), item("me/site", "Fix menu", 0)]
        return httpx.Response(200, json={"total_count": 12, "items": items})
    return httpx.Response(200, json={"total_count": 0, "items": []})


@respx.mock
async def test_groups_pull_requests_waiting_for_the_user(ctx):
    route = respx.get(f"{API_URL}/search/issues").mock(side_effect=search_response)
    async with httpx.AsyncClient() as http:
        (tool,) = build_github_tools(GitHub(http, "ghp_test"))
        result = await tool.handler({}, ctx)

    assert result["review_requested"]["count"] == 12
    assert result["review_requested"]["pull_requests"][0] == {
        "repo": "acme/api",
        "title": "Add invoices",
        "author": "alex",
        "draft": False,
        "open_for_days": 3,
    }
    assert result["approved_mine"] == {"count": 0, "pull_requests": []}
    assert route.call_count == len(QUERIES)
    assert route.calls[0].request.headers["authorization"] == "Bearer ghp_test"
    assert tool.taints


def test_no_token_means_no_tool():
    assert build_github_tools(None) == []
