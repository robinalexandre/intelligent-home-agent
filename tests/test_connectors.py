from __future__ import annotations

import json

import httpx
import pytest
import respx
from conftest import FakeLLM, text_response, tool_response

from intelligent_home_agent import main
from intelligent_home_agent.agent import Agent
from intelligent_home_agent.config import McpServer
from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.prompts import PromptConfig
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.connectors import Connector, build_connector_tools
from intelligent_home_agent.tools.mcp_client import MCPClient, OAuthToken, StaticToken
from intelligent_home_agent.tools.proposals import build_proposal_tools

URL = "https://mcp.bank.test/mcp"
SPECS = [
    {
        "name": "list_transactions",
        "description": "Transactions of an account. With filters.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"account": {"type": "string"}}},
    },
    {"name": "get_balance", "description": "Balances."},
    {
        "name": "create_client",
        "description": "Adds a client.",
        "annotations": {"destructiveHint": False},
    },
    {"name": "delete_client", "description": "Removes a client."},
    {"name": "bad name!", "description": "Ignored."},
]


class FakeServer:
    """An MCP server over respx: answers initialize, tools/list and tools/call."""

    def __init__(self, url: str = URL, answer: str = "ok") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.tokens: list[str | None] = []
        self.answer = answer
        respx.post(url).mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("authorization"))
        message = json.loads(request.content)
        method = message.get("method")
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            return httpx.Response(200, json=body, headers={"mcp-session-id": "s1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            result = {"tools": SPECS}
        else:
            self.calls.append((message["params"]["name"], message["params"]["arguments"]))
            result = {"content": [{"type": "text", "text": self.answer}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})


def connector_tools(http, *, writes=True, also_read=frozenset({"get_balance"}), language="en"):
    client = MCPClient(http, StaticToken("bank-token"), URL, "My Bank")
    connector = Connector("bank", client, writes=writes, also_read=also_read, language=language)
    return {t.name: t for t in build_connector_tools([connector])}


def context(turn: int = 1, conv: Conversation | None = None) -> ToolContext:
    conv = conv or Conversation(id="test")
    conv.turn = turn
    return ToolContext(conversation=conv)


@respx.mock
async def test_help_lists_the_server_tools_and_a_read_goes_through_and_taints():
    server = FakeServer(answer="12 transactions")
    async with httpx.AsyncClient() as http:
        tools = connector_tools(http)
        ctx = context()
        listing = await tools["bank_help"].handler({}, ctx)
        detail = await tools["bank_help"].handler({"tools": ["list_transactions", "nope"]}, ctx)
        result = await tools["bank_read"].handler(
            {"tool": "list_transactions", "arguments": {"account": "main"}}, ctx
        )
    assert "list_transactions (read): Transactions of an account" in listing
    assert "get_balance (read)" in listing and "delete_client (write)" in listing
    assert "bad name" not in listing
    assert '"account"' in detail and "No such tool" in detail
    assert result == "12 transactions"
    assert server.calls == [("list_transactions", {"account": "main"})]
    assert set(server.tokens) == {"Bearer bank-token"}
    assert tools["bank_read"].taints and not tools["bank_help"].taints


def registry_of(http, **kwargs) -> ToolRegistry:
    registry = ToolRegistry(list(connector_tools(http, **kwargs).values()))
    registry.extend(build_proposal_tools(registry, language=kwargs.get("language", "en")))
    return registry


@respx.mock
async def test_a_write_waits_for_a_yes_in_a_later_clean_message():
    server = FakeServer(answer="client created")
    call = {"tool": "create_client", "arguments": {"name": "Acme"}}
    async with httpx.AsyncClient() as http:
        registry = registry_of(http)
        conv = Conversation(id="test")
        ctx = context(1, conv)
        # Even in a clean request, a write never runs when asked: it is parked.
        parked = await registry.run("bank_write", call, ctx)
        assert parked["status"] == "waiting_for_the_user" and server.calls == []
        assert ctx.actions == [
            {"type": "proposal", "number": 1, "summary": 'My Bank: create_client {"name": "Acme"}'}
        ]
        # Not shown to the user yet: nothing can run it.
        with pytest.raises(ToolError, match="Nothing is waiting"):
            await registry.run("run_proposals", {}, ctx)
        main.with_waiting("Shall I?", ctx.actions, conv)
        # The model can't answer for the user in the same message.
        with pytest.raises(ToolError, match="has not answered yet"):
            await registry.run("run_proposals", {}, ctx)
        assert server.calls == []
        # Their next message, having read nothing: the write runs exactly as shown.
        done = await registry.run("run_proposals", {}, context(2, conv))
        assert done == {"done": [{"number": 1, "result": "client created"}], "failed": []}
        assert server.calls == [("create_client", {"name": "Acme"})]
        with pytest.raises(ToolError, match="Nothing is waiting"):
            await registry.run("run_proposals", {}, context(3, conv))
        # After a read in the same request, the go itself is refused.
        await registry.run("bank_write", call, context(4, conv))
        main.with_waiting("Shall I?", [{"type": "proposal", "number": 2, "summary": ""}], conv)
        tainted = context(5, conv)
        tainted.tainted = True
        with pytest.raises(ToolError, match="disabled in a request that read"):
            await registry.run("run_proposals", {}, tainted)
    assert len(server.calls) == 1


@respx.mock
async def test_a_destructive_write_says_so_and_unshowable_arguments_are_refused():
    server = FakeServer()
    async with httpx.AsyncClient() as http:
        registry = registry_of(http)
        ctx = context()
        await registry.run("bank_write", {"tool": "delete_client", "arguments": {"id": "7"}}, ctx)
        assert ctx.actions[0]["summary"] == (
            'My Bank (may delete or overwrite): delete_client {"id": "7"}'
        )
        # Refused before they are parked: the user is never asked about them.
        for arguments in ({"note": "x" * 700}, {"note": "a‮b"}):
            with pytest.raises(ToolError, match="too long, or hold hidden"):
                await registry.run(
                    "bank_write", {"tool": "create_client", "arguments": arguments}, ctx
                )
        with pytest.raises(ToolError, match="is a read: use bank_read"):
            await registry.run("bank_write", {"tool": "get_balance"}, ctx)
        assert len(ctx.conversation.proposals) == 1

        french = registry_of(http, language="fr")
        ctx = context()
        await french.run("bank_write", {"tool": "delete_client", "arguments": {"id": "7"}}, ctx)
        assert ctx.actions[0]["summary"] == (
            'My Bank (peut supprimer ou écraser) : delete_client {"id": "7"}'
        )
    assert server.calls == []


@respx.mock
async def test_reads_and_writes_cannot_be_swapped_and_read_only_has_no_write_tool():
    server = FakeServer()
    async with httpx.AsyncClient() as http:
        tools = connector_tools(http)
        with pytest.raises(ToolError, match="is a write: use bank_write"):
            await tools["bank_read"].handler({"tool": "delete_client"}, context())
        with pytest.raises(ToolError, match="no tool 'transfer'"):
            await tools["bank_read"].handler({"tool": "transfer"}, context())
        with pytest.raises(ToolError, match="as an object"):
            await tools["bank_read"].handler({"tool": "get_balance", "arguments": "x"}, context())

        read_only = connector_tools(http, writes=False)
        assert set(read_only) == {"bank_help", "bank_read"}
        assert "delete_client" not in await read_only["bank_help"].handler({}, context())
        with pytest.raises(ToolError, match="read-only here"):
            await read_only["bank_read"].handler({"tool": "create_client"}, context())
    assert server.calls == []


@respx.mock
async def test_the_reply_ends_with_the_exact_call_written_by_the_code(monkeypatch):
    server = FakeServer(answer="client created")
    async with httpx.AsyncClient() as http:
        registry = registry_of(http, language="fr")
        llm = FakeLLM(
            [
                tool_response(("c1", "bank_read", {"tool": "get_balance"})),
                tool_response(
                    ("c2", "bank_write", {"tool": "create_client", "arguments": {"name": "Acme"}})
                ),
                text_response("Shall I add Acme?"),
                tool_response(("c3", "run_proposals", {})),
                text_response("Done."),
            ]
        )
        agent = Agent(
            llm,
            registry,
            PromptConfig(assistant_name="A", language="fr", timezone="UTC"),
            model="m",
            max_tokens=100,
            max_tool_iterations=5,
            max_history_messages=40,
        )
        conv = Conversation(id="c")
        reply = await agent.ask(conv, "add Acme as a client")
        text = main.with_waiting(reply.text, reply.actions, conv, "fr")
        assert text == (
            "Shall I add Acme?\n\nEn attente de ton accord (« oui », ou « oui sauf 2 ») :\n"
            '1. My Bank : create_client {"name": "Acme"}'
        )
        assert server.calls == [("get_balance", {})]
        # The account was read: only the question and the model's words stay in memory.
        assert len(conv.messages) == 2 and "En attente" not in str(conv.messages)
        assert (await agent.ask(conv, "oui")).text == "Done."
        assert server.calls[-1] == ("create_client", {"name": "Acme"})


@respx.mock
async def test_oauth_tokens_client_credentials_then_a_rotating_refresh_token(tmp_path):
    token_url = "https://auth.test/token"
    route = respx.post(token_url).respond(json={"access_token": "a1", "expires_in": 3600})
    now = [0.0]
    async with httpx.AsyncClient() as http:
        auth = OAuthToken(
            http, "Svc", token_url, "cid", client_secret="shh", resource="r", clock=lambda: now[0]
        )
        assert await auth.access_token() == "a1" and await auth.access_token() == "a1"
        assert route.call_count == 1
        sent = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
        assert sent == {
            "client_id": "cid",
            "grant_type": "client_credentials",
            "client_secret": "shh",
            "resource": "r",
        }

        store = tmp_path / "mcp-svc.json"
        respx.post(token_url).respond(
            json={"access_token": "a2", "expires_in": 60, "refresh_token": "r2"}
        )
        auth = OAuthToken(http, "Svc", token_url, "cid", refresh_token="r1", store=store)
        assert await auth.access_token() == "a2"
        assert json.loads(store.read_text())["refresh_token"] == "r2"
        assert store.stat().st_mode & 0o777 == 0o600
        # After a restart the rotated token is used; a new login in the settings wins.
        again = OAuthToken(http, "Svc", token_url, "cid", refresh_token="r1", store=store)
        await again.access_token(force=True)
        sent = dict(httpx.QueryParams(respx.calls.last.request.content.decode()))
        assert sent["refresh_token"] == "r2" and sent["grant_type"] == "refresh_token"
        fresh = OAuthToken(http, "Svc", token_url, "cid", refresh_token="r9", store=store)
        await fresh.access_token()
        sent = dict(httpx.QueryParams(respx.calls.last.request.content.decode()))
        assert sent["refresh_token"] == "r9"

        respx.post(token_url).respond(401)
        with pytest.raises(ToolError, match="mcp_login.py again"):
            await fresh.access_token(force=True)


def test_settings_build_one_connector_per_server(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_API_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    servers = [
        {"name": "bank", "label": "My Bank", "url": URL, "token": "t", "writes": "confirm"},
        {
            "name": "hosting",
            "url": "https://mcp.host.test/mcp",
            "client_id": "cid",
            "refresh_token": "r1",
            "token_url": "https://mcp.host.test/token",
        },
    ]
    monkeypatch.setenv("MCP_SERVERS", json.dumps(servers))
    settings = main.Settings(_env_file=None)
    names = main.build_registry(settings, None, None, None).names()
    assert {"bank_help", "bank_read", "bank_write", "run_proposals"} <= names
    assert {"hosting_help", "hosting_read"} <= names and "hosting_write" not in names
    assert "t" not in json.dumps(settings.model_dump(mode="json")["mcp_servers"][0]["token"])

    for bad, match in (
        ([servers[0], servers[0]], "share a name"),
        ([{"name": "Bank!", "url": URL}], "pattern"),
        ([{"name": "x", "url": "ftp://x"}], "must start with https"),
        ([{"name": "x", "url": URL, "client_id": "c"}], "client_id and token_url"),
        ([{"name": "x", "url": URL, "client_id": "c", "token_url": URL}], "client_secret or"),
    ):
        monkeypatch.setenv("MCP_SERVERS", json.dumps(bad))
        with pytest.raises(ValueError, match=match):
            main.Settings(_env_file=None)


def load_login():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "mcp_login.py"
    spec = importlib.util.spec_from_file_location("mcp_login", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_login_script_finds_the_endpoints_and_writes_a_loadable_entry():
    login = load_login()
    pages = {
        "https://mcp.bank.test/.well-known/oauth-protected-resource": {
            "resource": "https://mcp.bank.test",
            "authorization_servers": ["https://auth.bank.test/"],
        },
        "https://auth.bank.test/.well-known/oauth-authorization-server": {
            "authorization_endpoint": "https://auth.bank.test/oauth/authorize",
            "token_endpoint": "https://auth.bank.test/oauth/token",
            "scopes_supported": ["read", "offline_access"],
        },
    }

    def get(url, data=None, form=False):
        if url not in pages:
            raise OSError("404")
        return pages[url]

    found = login.discover(URL, get)
    assert found == {
        "authorize": "https://auth.bank.test/oauth/authorize",
        "token": "https://auth.bank.test/oauth/token",
        "register": "https://auth.bank.test/register",
        "resource": "https://mcp.bank.test",
        "scopes": "read offline_access",
    }
    # A server that publishes nothing: the usual paths on its own address.
    assert login.discover(URL, lambda *a: (_ for _ in ()).throw(OSError("no")))["token"] == (
        "https://mcp.bank.test/token"
    )
    made = login.entry("bank", "My Bank", URL, found, "cid", "r1", found["scopes"])
    server = McpServer(**made)
    assert server.writes == "off" and server.token_url == found["token"]
    with pytest.raises(OSError, match="not HTTPS"):
        login.fetch("http://mcp.bank.test/register")
