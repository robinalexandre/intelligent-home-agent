import base64

import httpx
import pytest
import respx

from intelligent_home_agent.config_crypt import ConfigCrypt
from intelligent_home_agent.tools import Repositories, ToolError, build_repository_tools
from intelligent_home_agent.tools.github import API_URL

CONFIG = f"{API_URL}/repos/me/config/contents"


def tools(http):
    return {t.name: t for t in build_repository_tools(Repositories(http, "ghp_test", "me/config"))}


@respx.mock
async def test_browses_the_notes_repository_by_default(ctx):
    respx.get(f"{CONFIG}/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "README.md", "type": "file"},
                {"name": "reminders", "type": "dir"},
            ],
        )
    )
    note = "# Handoff\nDeploy with docker compose."
    route = respx.get(f"{CONFIG}/handoffs/vps.md").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "path": "handoffs/vps.md",
                "content": base64.b64encode(note.encode()).decode(),
            },
        )
    )
    async with httpx.AsyncClient() as http:
        browse = tools(http)["browse_repository"]
        folder = await browse.handler({}, ctx)
        file = await browse.handler({"path": "/handoffs/vps.md"}, ctx)

    assert folder == {
        "repo": "me/config",
        "folder": "/",
        "entries": [{"name": "reminders", "type": "dir"}, {"name": "README.md", "type": "file"}],
    }
    assert file == {
        "repo": "me/config",
        "file": "handoffs/vps.md",
        "content": note,
        "truncated": False,
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer ghp_test"
    assert browse.taints


@respx.mock
async def test_searches_another_repo(ctx):
    route = respx.get(f"{API_URL}/search/code").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"path": "deploy.md", "text_matches": [{"fragment": "ssh root@"}]}]},
        )
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["search_repository"].handler(
            {"query": "deploy", "repo": "me/site"}, ctx
        )

    assert result == {
        "repo": "me/site",
        "results": [{"path": "deploy.md", "matches": ["ssh root@"]}],
    }
    assert route.calls[0].request.url.params["q"] == "deploy repo:me/site"


@respx.mock
async def test_missing_path_is_a_clear_error(ctx):
    respx.get(f"{CONFIG}/nope").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="Not found"):
            await tools(http)["browse_repository"].handler({"path": "nope"}, ctx)


async def test_repo_must_be_owner_and_name(ctx):
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="owner/name"):
            await tools(http)["browse_repository"].handler({"repo": "config"}, ctx)


def test_no_repositories_means_no_tools():
    assert build_repository_tools(None) == []


@respx.mock
async def test_without_a_notes_repository_the_repo_is_asked_for(ctx):
    respx.get(f"{API_URL}/user/repos").mock(
        return_value=httpx.Response(
            200, json=[{"full_name": "me/site", "private": True, "pushed_at": "2026-10-01T08:00"}]
        )
    )
    async with httpx.AsyncClient() as http:
        named = {t.name: t for t in build_repository_tools(Repositories(http, "t"))}
        listed = await named["list_repositories"].handler({}, ctx)
        with pytest.raises(ToolError, match="owner/name"):
            await named["browse_repository"].handler({}, ctx)

    assert listed["notes_repo"] is None
    assert listed["repositories"][0] == {
        "repo": "me/site",
        "description": None,
        "private": True,
        "last_push": "2026-10-01",
    }
    assert "repo" in named["search_repository"].input_schema["required"]
    assert all(t.taints and not t.needs_clean_turn for t in named.values())


@respx.mock
async def test_an_encrypted_file_is_read_with_the_key_and_refused_without(ctx):
    from test_config_crypt import ENCRYPTED, KEY, PLAIN

    respx.get(f"{CONFIG}/notes/note.md").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "path": "notes/note.md",
                "content": base64.b64encode(ENCRYPTED).decode(),
            },
        )
    )
    crypt = ConfigCrypt.from_base64(KEY, ["notes/**"])
    async with httpx.AsyncClient() as http:
        repos = Repositories(http, "t", "me/config", crypt=crypt)
        (browse,) = [t for t in build_repository_tools(repos) if t.name == "browse_repository"]
        file = await browse.handler({"path": "notes/note.md"}, ctx)
        with pytest.raises(ToolError, match="no key"):
            await tools(http)["browse_repository"].handler({"path": "notes/note.md"}, ctx)

    assert file["content"] == PLAIN.decode()
