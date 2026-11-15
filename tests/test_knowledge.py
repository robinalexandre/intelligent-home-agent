import httpx
import pytest
import respx

from intelligent_home_agent.tools import KnowledgeBase, ToolError, build_knowledge_tools

URL = "http://kb:8800"
HIT = {
    "path": "/data/.claude-sync/knowledge/server-layout.md",
    "name": "server-layout",
    "title": "Where the agent runs",
    "type": "fact",
    "trust": "user",
    "score": 0.03,
    "snippet": "the agent runs on the server " * 20,
    "topics": ["server"],
    "valid_until": None,
    "source": "curator, session:abcd",
}
RUMOR = {**HIT, "name": "rumor", "trust": "third-party", "source": "mail"}


def tools(http):
    return {t.name: t for t in build_knowledge_tools(KnowledgeBase(http, URL, "kb-token"))}


@respx.mock
async def test_recall_leaves_third_party_notes_out_and_marks_curated_ones(ctx):
    route = respx.get(f"{URL}/search").mock(return_value=httpx.Response(200, json={"hits": [HIT]}))
    async with httpx.AsyncClient() as http:
        result = await tools(http)["recall"].handler(
            {"query": "where does  the agent run", "types": ["fact"], "limit": 3}, ctx
        )
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer kb-token"
    params = httpx.QueryParams(request.url.query)
    assert params["q"] == "where does the agent run" and params["n"] == "3"
    assert params.get_list("trust") == ["user", "agent"] and params["type"] == "fact"
    note = result["notes"][0]
    assert note["name"] == "server-layout" and note["written_by"] == "curator"
    assert len(note["snippet"]) <= 300 and "valid_until" not in note
    assert not ctx.tainted


@respx.mock
async def test_recall_with_third_party_notes_taints_the_request(ctx):
    route = respx.get(f"{URL}/search").mock(
        return_value=httpx.Response(200, json={"hits": [HIT, RUMOR]})
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["recall"].handler(
            {"query": "bank fees", "include_third_party": True}, ctx
        )
    assert "trust" not in httpx.QueryParams(route.calls[0].request.url.query)
    assert [n["name"] for n in result["notes"]] == ["server-layout", "rumor"]
    assert ctx.tainted


@respx.mock
async def test_related_and_read_note(ctx):
    respx.get(f"{URL}/related").mock(
        return_value=httpx.Response(
            200,
            json={
                "nodes": [
                    {
                        "name": "server-layout",
                        "depth": 0,
                        "title": "Server",
                        "type": "fact",
                        "via": "link",
                    },
                    {"name": "gone", "depth": 1, "title": "", "type": "missing", "via": "link"},
                    {
                        "name": "backups",
                        "depth": 1,
                        "title": "B",
                        "type": "fact",
                        "via": "topic server",
                    },
                ],
                "edges": [["server-layout", "gone"]],
            },
        )
    )
    respx.get(f"{URL}/notes/server-layout").mock(
        return_value=httpx.Response(
            200,
            json={
                **HIT,
                "body": "Long body. " * 1000,
                "created": "2026-10-22",
                "hash": "x",
            },
        )
    )
    respx.get(f"{URL}/notes/nobody").mock(return_value=httpx.Response(404, json={"error": "no"}))
    async with httpx.AsyncClient() as http:
        kb = tools(http)
        related = await kb["related_notes"].handler({"name": "Server-Layout", "depth": 9}, ctx)
        note = await kb["read_note"].handler({"name": "server-layout"}, ctx)
        with pytest.raises(ToolError):
            await kb["read_note"].handler({"name": "nobody"}, ctx)
    assert [n["name"] for n in related["notes"]] == ["server-layout", "backups"]
    assert related["links"] == [["server-layout", "gone"]]
    assert note["written_by"] == "curator" and note["created"] == "2026-10-22"
    assert "hash" not in note and "score" not in note and note["body"].endswith(" […]")
    assert not ctx.tainted


@respx.mock
async def test_errors_are_tool_errors(ctx):
    respx.get(f"{URL}/search").mock(return_value=httpx.Response(401, json={"error": "token"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="HTTP 401 token"):
            await tools(http)["recall"].handler({"query": "x"}, ctx)
        with pytest.raises(ToolError, match="required"):
            await tools(http)["recall"].handler({"query": "  "}, ctx)


def test_no_tools_without_a_server():
    assert build_knowledge_tools(None) == []
