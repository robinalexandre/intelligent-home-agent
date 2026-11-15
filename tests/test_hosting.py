import json

import httpx
import pytest
import respx

from intelligent_home_agent.config import Settings
from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.main import with_waiting
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry, build_proposal_tools
from intelligent_home_agent.tools.hosting import (
    PROVIDERS,
    READS,
    WRITES,
    Hetzner,
    Hostinger,
    build_hosting_provider,
    build_hosting_tools,
    risky_settings,
)

VM = f"{Hostinger.url}/api/vps/v1/virtual-machines"
HETZNER = Hetzner.url


def registry(api, language="en"):
    tools = ToolRegistry(build_hosting_tools(api, language))
    tools.extend(build_proposal_tools(tools))
    return tools


@respx.mock
async def test_reading_is_free_and_taints(ctx):
    route = respx.get(f"{VM}/7/metrics").respond(json={"cpu_usage": {"usage": {}}})
    async with httpx.AsyncClient() as http:
        tools = registry(Hostinger(http, "tok"))
        result = await tools.run("get_server_metrics", {"server_id": "7", "hours": 2}, ctx)

    assert json.loads(result) == {"cpu_usage": {"usage": {}}}
    params = route.calls[0].request.url.params
    assert params["date_from"] < params["date_to"]
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert tools.taints("get_server_project_logs")


@respx.mock
async def test_a_restart_asked_by_the_user_runs_otherwise_it_waits(ctx):
    respx.get(f"{VM}/7").respond(json={"id": 7, "hostname": "srv1.example.com"})
    restart = respx.post(f"{VM}/7/docker/site/restart").respond(json={"id": 1})
    args = {"server_id": "7", "project": "site"}
    async with httpx.AsyncClient() as http:
        tools = registry(Hostinger(http, "tok"))
        await tools.run("restart_server_project", args, ctx)
        assert restart.call_count == 1  # the user's own clear request

        after_logs = ToolContext(conversation=ctx.conversation, tainted=True)
        parked = await tools.run("restart_server_project", args, after_logs)

    assert parked["status"] == "waiting_for_the_user" and restart.call_count == 1
    assert after_logs.actions[0]["summary"] == (
        "Hostinger: restart “site” on the server srv1.example.com"
    )


@respx.mock
async def test_an_action_waits_for_the_user_in_a_scheduled_task(ctx):
    respx.get(f"{VM}/7").respond(json={"id": 7, "hostname": "srv1"})
    restart = respx.post(f"{VM}/7/restart").respond(json={"id": 1})
    async with httpx.AsyncClient() as http:
        tools = registry(Hostinger(http, "tok"))
        task = ToolContext(conversation=ctx.conversation, scheduled=True)
        parked = await tools.run("restart_server", {"server_id": "7"}, task)

    assert parked["status"] == "waiting_for_the_user" and not restart.called
    assert task.actions[0]["summary"] == "Hostinger: restart the whole server on the server srv1"


@respx.mock
async def test_a_deployment_always_waits_and_shows_its_compose_file(ctx):
    respx.get(f"{VM}/7").respond(json={"id": 7, "hostname": "srv1"})
    deploy = respx.post(f"{VM}/7/docker").respond(json={"id": 2})
    compose = "services:\n  web:\n    image: nginx:1.27"
    async with httpx.AsyncClient() as http:
        tools = registry(Hostinger(http, "tok"))
        parked = await tools.run(
            "deploy_server_project", {"server_id": "7", "project": "site", "content": compose}, ctx
        )
        summary = ctx.actions[0]["summary"]
        with_waiting("", ctx.actions, ctx.conversation)
        ctx.conversation.turn += 1
        await tools.run("run_proposals", {}, ToolContext(conversation=ctx.conversation))

    assert parked["status"] == "waiting_for_the_user"
    assert summary == (
        "Hostinger: deploy the Docker project “site” on the server srv1; "
        "compose: services: ⏎ web: ⏎ image: nginx:1.27"
    )
    assert json.loads(deploy.calls[0].request.content) == {
        "project_name": "site",
        "content": compose,
    }


@respx.mock
async def test_a_risky_compose_is_flagged_in_the_users_language_and_a_link_is_refused(ctx):
    respx.get(f"{VM}/7").respond(json={"id": 7, "hostname": "srv1"})
    deploy = respx.post(f"{VM}/7/docker").respond(json={"id": 2})
    risky = "services:\n  x:\n    privileged: true\n    volumes:\n      - /:/host"
    args = {"server_id": "7", "project": "x", "content": risky}
    french = ToolContext(conversation=Conversation(id="fr", turn=1))
    async with httpx.AsyncClient() as http:
        await registry(Hostinger(http, "tok")).run("deploy_server_project", args, ctx)
        await registry(Hostinger(http, "tok"), "fr").run("deploy_server_project", args, french)
        with pytest.raises(ToolError, match="not a link"):
            await registry(Hostinger(http, "tok")).run(
                "deploy_server_project",
                {**args, "content": "https://files.example.com/c.yml"},
                ctx,
                approved=True,
            )

    assert "; ⚠️ privileged, a folder of the host mounted; compose:" in ctx.actions[0]["summary"]
    assert french.actions[0]["summary"].startswith(
        "Hostinger : déployer le projet Docker « x » sur le serveur srv1 ; "
        "⚠️ privileged, dossier de l'hôte monté ; compose : "
    )
    assert not deploy.called


def test_every_risky_setting_has_its_words_in_each_language():
    from intelligent_home_agent.i18n import LANGUAGES

    everything = (
        "privileged: true\nnetwork_mode: host\npid: 'host'\ncap_add:\n  - NET_ADMIN\n"
        "volumes:\n  - /var/run/docker.sock:/var/run/docker.sock"
    )
    keys = risky_settings(everything)
    assert keys == [
        "privileged",
        "host_network",
        "host_pid",
        "cap_add",
        "docker_socket",
        "host_folder",
    ]
    assert risky_settings("services:\n  web:\n    volumes:\n      - data:/data") == []
    actions = {what for *_, what, _ in WRITES} - {"snapshot"}
    actions |= {"snapshot_replacing", "snapshot_kept", "deploy_project"}
    for words in LANGUAGES.values():
        assert set(words.hosting_risky) == set(keys)
        assert set(words.hosting_actions) == actions


async def test_line_breaks_only_where_a_tool_shows_them(ctx):
    async with httpx.AsyncClient() as http:
        tools = registry(Hostinger(http, "tok"))
        after_logs = ToolContext(conversation=ctx.conversation, tainted=True)
        with pytest.raises(ToolError, match="hidden characters"):
            await tools.run(
                "restart_server_project", {"server_id": "7", "project": "a\nb"}, after_logs
            )


@pytest.mark.parametrize("args", [{"server_id": "../7"}, {"server_id": "7", "project": "a/../b"}])
async def test_odd_ids_never_reach_the_api(args, ctx):
    async with httpx.AsyncClient() as http:
        tools = registry(Hostinger(http, "tok"))
        with pytest.raises(ToolError):
            await tools.run("list_server_project_containers", {"project": "x", **args}, ctx)


def test_no_provider_no_tools():
    assert build_hosting_tools(None) == []
    assert build_hosting_provider("", None, None) is None
    assert build_hosting_provider("hostinger", None, None) is None


def test_a_provider_needs_its_token_and_must_be_known(monkeypatch):
    for name in ("HOSTING_PROVIDER", "HOSTING_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    base = {"agent_api_token": "t", "llm_api_key": "k", "_env_file": None}
    assert Settings(**base).hosting_provider == ""
    with pytest.raises(ValueError, match="HOSTING_API_TOKEN"):
        Settings(**base, hosting_provider="hetzner")
    with pytest.raises(ValueError):
        Settings(**base, hosting_provider="elsewhere", hosting_api_token="t")
    assert set(PROVIDERS) == {"hostinger", "hetzner"}


@respx.mock
async def test_a_read_failing_on_the_providers_side_is_retried_once(ctx):
    route = respx.get(f"{VM}/7/docker").mock(
        side_effect=[
            httpx.Response(500, json={"message": "[VPS:7] Request failed"}),
            httpx.Response(200, json=[{"name": "site"}]),
        ]
    )
    async with httpx.AsyncClient() as http:
        tools = ToolRegistry(build_hosting_tools(Hostinger(http, "tok", retry_seconds=0)))
        result = await tools.run("list_server_projects", {"server_id": "7"}, ctx)

    assert json.loads(result) == [{"name": "site"}] and route.call_count == 2


@respx.mock
async def test_a_lasting_outage_is_one_short_error_without_the_upstream_body():
    body = {"message": "cURL error 7 for http://sso.internal.example/api/internal/token"}
    route = respx.get(VM).respond(500, json=body)
    async with httpx.AsyncClient() as http:
        api = Hostinger(http, "tok", retry_seconds=0)
        with pytest.raises(ToolError) as err:
            await api.list_servers()

    assert route.call_count == 2
    assert "internal.example" not in str(err.value)
    assert "don't repeat this call" in str(err.value)


@respx.mock
async def test_an_action_failing_on_the_providers_side_is_never_sent_twice():
    route = respx.post(f"{VM}/7/snapshot").respond(502)
    async with httpx.AsyncClient() as http:
        api = Hostinger(http, "tok", retry_seconds=0)
        with pytest.raises(ToolError, match="may have run anyway"):
            await api.take_snapshot("7")

    assert route.call_count == 1


@respx.mock
async def test_a_refused_token_says_where_to_make_another():
    respx.get(VM).respond(401)
    respx.get(f"{HETZNER}/servers").respond(401, json={"error": {"code": "unauthorized"}})
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="hPanel"):
            await Hostinger(http, "tok").list_servers()
        with pytest.raises(ToolError, match="Cloud Console"):
            await Hetzner(http, "tok").list_servers()


# Hetzner Cloud: written from its documentation, so these check the calls it describes.


def test_a_provider_only_has_the_tools_it_can_serve():
    hostinger = {t.name for t in build_hosting_tools(Hostinger(None, "tok"))}
    hetzner = {t.name for t in build_hosting_tools(Hetzner(None, "tok"))}

    assert hostinger == {name for name, *_ in READS + WRITES} | {"deploy_server_project"}
    assert hetzner == {
        "list_servers",
        "get_server",
        "get_server_metrics",
        "list_server_actions",
        "list_server_backups",
        "list_server_firewalls",
        "restart_server",
        "take_server_snapshot",
    }


@respx.mock
async def test_hetzner_reads_servers_metrics_and_firewalls(ctx):
    servers = respx.get(f"{HETZNER}/servers").respond(json={"servers": [{"id": 42}]})
    metrics = respx.get(f"{HETZNER}/servers/42/metrics").respond(json={"metrics": {}})
    respx.get(f"{HETZNER}/firewalls").respond(json={"firewalls": []})
    async with httpx.AsyncClient() as http:
        tools = registry(Hetzner(http, "tok"))
        listed = await tools.run("list_servers", {}, ctx)
        await tools.run("get_server_metrics", {"server_id": "42", "hours": 6}, ctx)
        await tools.run("list_server_firewalls", {}, ctx)

    assert json.loads(listed) == {"servers": [{"id": 42}]}
    assert servers.calls[0].request.headers["authorization"] == "Bearer tok"
    params = metrics.calls[0].request.url.params
    assert params["type"] == "cpu,disk,network"
    assert params["start"] < params["end"] and params["step"] == "360"
    assert tools.taints("list_servers")


@respx.mock
async def test_hetzner_backups_are_the_servers_own_images(ctx):
    images = respx.get(f"{HETZNER}/images").mock(
        side_effect=[
            httpx.Response(200, json={"images": [{"id": 1, "type": "backup"}]}),
            httpx.Response(
                200,
                json={
                    "images": [
                        {"id": 2, "created_from": {"id": 42, "name": "web"}},
                        {"id": 3, "created_from": {"id": 43, "name": "other"}},
                    ]
                },
            ),
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await registry(Hetzner(http, "tok")).run(
            "list_server_backups", {"server_id": "42"}, ctx
        )

    assert dict(images.calls[0].request.url.params) == {"type": "backup", "bound_to": "42"}
    assert json.loads(result) == {
        "backups": [{"id": 1, "type": "backup"}],
        "snapshots": [{"id": 2, "created_from": {"id": 42, "name": "web"}}],
    }


@respx.mock
async def test_hetzner_reboot_and_snapshot_wait_after_a_read_and_say_what_a_snapshot_costs(ctx):
    respx.get(f"{HETZNER}/servers/42").respond(json={"server": {"id": 42, "name": "web"}})
    reboot = respx.post(f"{HETZNER}/servers/42/actions/reboot").respond(json={"action": {}})
    image = respx.post(f"{HETZNER}/servers/42/actions/create_image").respond(json={"image": {}})
    async with httpx.AsyncClient() as http:
        tools = registry(Hetzner(http, "tok"))
        await tools.run("restart_server", {"server_id": "42"}, ctx)
        after_read = ToolContext(conversation=ctx.conversation, tainted=True)
        parked = await tools.run("take_server_snapshot", {"server_id": "42"}, after_read)
        assert not image.called
        with_waiting("", after_read.actions, ctx.conversation)
        ctx.conversation.turn += 1
        await tools.run("run_proposals", {}, ToolContext(conversation=ctx.conversation))

    assert reboot.call_count == 1 and parked["status"] == "waiting_for_the_user"
    assert after_read.actions[0]["summary"] == (
        "Hetzner: take a snapshot (kept, and billed, until you delete it) on the server web"
    )
    assert json.loads(image.calls[0].request.content)["type"] == "snapshot"
