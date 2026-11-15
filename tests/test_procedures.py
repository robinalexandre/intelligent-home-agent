import base64
import json

import httpx
import pytest
import respx

from intelligent_home_agent.tools import (
    GitHubFile,
    LocalFile,
    Memory,
    ToolError,
    ToolRegistry,
    build_procedure_tools,
)
from intelligent_home_agent.tools.github import API_URL

URL = f"{API_URL}/repos/me/config/contents/nova/procedures.md"
STEPS = "1. Open energy.example.com/documents\n2. Click on Bills\n3. Download the PDF"


class Repo:
    """The file in a fake repository: what was written is what is read next."""

    def __init__(self):
        self.text = None

    def get(self, _request):
        if self.text is None:
            return httpx.Response(404)
        content = base64.b64encode(self.text.encode()).decode()
        return httpx.Response(200, json={"content": content, "sha": "s"})

    def put(self, request):
        self.text = base64.b64decode(json.loads(request.content)["content"]).decode()
        return httpx.Response(200, json={})


async def keep_find_replace(book, ctx):
    tools = {t.name: t for t in build_procedure_tools(book, "UTC", "Nova")}
    args = {"site": "energy.example.com", "job": "download the bill", "steps": STEPS}
    assert (await tools["save_procedure"].handler(args, ctx))["result"] == "saved"
    bank = {"site": "bank.example.com", "job": "statement", "steps": "1. Open the app"}
    await tools["save_procedure"].handler(bank, ctx)
    newer = {**args, "steps": "1. Open energy.example.com/billing"}
    assert (await tools["save_procedure"].handler(newer, ctx))["result"] == "updated"
    return await tools["find_procedure"].handler({"query": "Energy BILL"}, ctx)


def check(text, found):
    assert text.startswith("# Nova — procedures that worked")
    assert text.count("## energy.example.com") == 1
    assert "## bank.example.com — statement" in text
    (energy,) = found["procedures"]
    assert "energy.example.com/billing" in energy and "Bills" not in energy


@respx.mock
async def test_a_procedure_that_worked_is_kept_found_and_replaced_in_a_repository(ctx):
    repo = Repo()
    respx.get(URL).mock(side_effect=repo.get)
    respx.put(URL).mock(side_effect=repo.put)
    async with httpx.AsyncClient() as http:
        book = Memory(GitHubFile(http, "t", "me/config", "nova/procedures.md"), "UTC", "Nova")
        found = await keep_find_replace(book, ctx)
    check(repo.text, found)


async def test_a_procedure_that_worked_is_kept_found_and_replaced_in_a_local_file(tmp_path, ctx):
    path = tmp_path / "procedures.md"
    found = await keep_find_replace(Memory(LocalFile(path), "UTC", "Nova"), ctx)
    check(path.read_text(), found)


async def test_after_browsing_a_procedure_waits_shown_with_every_step(tmp_path, ctx):
    path = tmp_path / "procedures.md"
    registry = ToolRegistry(
        build_procedure_tools(Memory(LocalFile(path), "UTC"), "UTC", language="fr")
    )
    ctx.read_page("energy.example.com")
    args = {"site": "energy.example.com", "job": "bill", "steps": STEPS}
    parked = await registry.run("save_procedure", args, ctx)
    assert parked["status"] == "waiting_for_the_user" and not path.exists()
    summary = ctx.actions[0]["summary"]
    assert summary.startswith("Retenir la démarche — ")
    assert "2. Click on Bills ⏎ 3. Download the PDF" in summary


async def test_a_procedure_never_holds_a_password_and_needs_every_field(tmp_path, ctx):
    book = Memory(LocalFile(tmp_path / "procedures.md"), "UTC")
    tools = {t.name: t for t in build_procedure_tools(book, "UTC")}
    args = {"site": "energy.example.com", "job": "bill"}
    with pytest.raises(ToolError, match="Never keep"):
        await tools["save_procedure"].handler({**args, "steps": "2. Type the password x1"}, ctx)
    with pytest.raises(ToolError, match="required"):
        await tools["save_procedure"].handler(args, ctx)
    with pytest.raises(ToolError, match="under 3000"):
        await tools["save_procedure"].handler({**args, "steps": "x" * 3001}, ctx)
    with pytest.raises(ToolError, match="site or the job"):
        await tools["find_procedure"].handler({"query": "a"}, ctx)
    assert await tools["find_procedure"].handler({"query": "energy"}, ctx) == {"procedures": []}
    assert build_procedure_tools(None, "UTC") == []
