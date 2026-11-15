import base64
import json

import httpx
import pytest
import respx
from test_config_crypt import ENCRYPTED, KEY, PLAIN

from intelligent_home_agent.config_crypt import ConfigCrypt
from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.tools import (
    GitHubFile,
    LocalFile,
    Memory,
    ToolContext,
    ToolError,
    ToolRegistry,
    build_memory_tools,
)
from intelligent_home_agent.tools.github import API_URL
from intelligent_home_agent.tools.memory import folder_name

URL = f"{API_URL}/repos/me/config/contents/nova/memory.md"
EXISTING = "# Nova — long-term memory\n\n- My accountant is Jo Martin (2026-10-01)\n"


def file_response(text, sha="s1"):
    raw = text if isinstance(text, bytes) else text.encode()
    return httpx.Response(200, json={"content": base64.b64encode(raw).decode(), "sha": sha})


def sent(route) -> bytes:
    return base64.b64decode(json.loads(route.calls[-1].request.content)["content"])


def written(route):
    body = json.loads(route.calls[-1].request.content)
    return sent(route).decode(), body.get("sha")


def memory(http, crypt=None):
    return Memory(GitHubFile(http, "t", "me/config", "nova/memory.md", crypt), "UTC", "Nova")


def local(tmp_path):
    return Memory(LocalFile(tmp_path / "data" / "memory.md"), "UTC", "Nova")


def test_the_folder_of_a_repository_is_named_after_the_assistant():
    assert folder_name("Nova") == "nova"
    assert folder_name("Mrs. Hudson 2") == "mrs-hudson-2"
    assert folder_name("…") == "assistant"


# --- A local file (the default) ------------------------------------------------------------


async def test_a_local_memory_is_created_read_and_edited(tmp_path, ctx):
    mem = local(tmp_path)
    assert await mem.prompt_text() == ""
    tools = {t.name: t for t in build_memory_tools(mem)}

    result = await tools["remember"].handler({"fact": "Always copy  Jo\n on the quotes"}, ctx)

    file = tmp_path / "data" / "memory.md"
    text = file.read_text()
    assert result == {"result": "remembered"}
    assert text.startswith("# Nova — long-term memory")
    assert text.splitlines()[-1].startswith("- Always copy Jo on the quotes (")
    assert file.stat().st_mode & 0o777 == 0o600
    # The new fact is in the next prompt at once, and telling it again changes nothing.
    assert (await mem.prompt_text()).startswith("- Always copy Jo on the quotes (")
    again = await tools["remember"].handler({"fact": "Always copy Jo on the quotes"}, ctx)
    assert again == {"result": "already known"} and file.read_text() == text

    assert await tools["forget"].handler({"words": "QUOTES"}, ctx) == {"removed": 1}
    assert "Jo" not in file.read_text() and await mem.prompt_text() == ""
    with pytest.raises(ToolError, match="few words"):
        await mem.forget("jo")


async def test_a_line_the_user_wrote_by_hand_is_read_and_kept(tmp_path):
    file = tmp_path / "data" / "memory.md"
    file.parent.mkdir()
    file.write_text("# My notes\n\n- I live in Springfield\nnot a fact\n")
    mem = local(tmp_path)
    assert await mem.prompt_text() == "- I live in Springfield"
    await mem.remember("My bike is red")
    assert file.read_text().startswith("# My notes\n\n- I live in Springfield\nnot a fact\n- My")


async def test_a_local_edit_made_meanwhile_is_kept(tmp_path):
    path = tmp_path / "memory.md"
    path.write_text(EXISTING)
    file = LocalFile(path)
    text, version = await file.read()
    path.write_text(EXISTING + "- Added by hand\n")
    assert not await file.write(text + "- Mine\n", version, "")
    await Memory(file, "UTC").remember("New fact")
    assert "- Added by hand\n- New fact (" in path.read_text()


async def test_only_the_newest_facts_fit_the_prompt(tmp_path):
    path = tmp_path / "memory.md"
    path.write_text("".join(f"- fact {i} {'x' * 280}\n" for i in range(40)))
    text = await Memory(LocalFile(path), "UTC").prompt_text()
    assert len(text) <= 6000 and text.splitlines()[-1].startswith("- fact 39 ")
    assert "- fact 0 " not in text


# --- A path of a GitHub repository ----------------------------------------------------------


@respx.mock
async def test_facts_are_read_for_the_prompt():
    respx.get(URL).mock(return_value=file_response(EXISTING))
    async with httpx.AsyncClient() as http:
        assert await memory(http).prompt_text() == "- My accountant is Jo Martin (2026-10-01)"


@respx.mock
async def test_an_unreachable_repository_leaves_the_prompt_without_memory():
    respx.get(URL).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        mem = memory(http)
        assert await mem.prompt_text() == ""
        with pytest.raises(ToolError, match="HTTP 500"):
            await mem.remember("Something")


@respx.mock
async def test_remember_appends_a_dated_line_and_forget_removes_it(ctx):
    respx.get(URL).mock(return_value=file_response(EXISTING))
    put = respx.put(URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        tools = {t.name: t for t in build_memory_tools(memory(http))}
        result = await tools["remember"].handler({"fact": "Always copy  Sam\n on the quotes"}, ctx)
        text, sha = written(put)
        assert result == {"result": "remembered"}
        assert text.splitlines()[-1].startswith("- Always copy Sam on the quotes (")
        assert sha == "s1"
        message = json.loads(put.calls[-1].request.content)["message"]
        assert message == "chore(memory): Nova remembers a fact"

        removed = await tools["forget"].handler({"words": "accountant"}, ctx)
        text, _ = written(put)
        assert removed == {"removed": 1} and "Jo" not in text

    assert tools["remember"].needs_clean_turn and tools["forget"].needs_clean_turn


@respx.mock
async def test_a_fact_told_in_a_quoted_reply_waits_for_the_users_go():
    respx.get(URL).mock(return_value=file_response(EXISTING))
    put = respx.put(URL).mock(return_value=httpx.Response(200, json={}))
    fact = {"fact": "Not free to sail before Wednesday"}
    async with httpx.AsyncClient() as http:
        registry = ToolRegistry(build_memory_tools(memory(http), "fr"))
        quoted = ToolContext(conversation=Conversation(id="c", turn=1), quoted="own")

        parked = await registry.run("remember", fact, quoted)

        assert parked["status"] == "waiting_for_the_user" and not put.called
        assert quoted.actions[0]["summary"] == "Retenir « Not free to sail before Wednesday »"
        assert await registry.run("remember", fact, quoted, approved=True) == {
            "result": "remembered"
        }
        assert "Not free to sail before Wednesday" in written(put)[0]


async def test_after_a_read_nothing_is_remembered_or_forgotten_without_a_go(tmp_path, ctx):
    mem = local(tmp_path)
    await mem.remember("My accountant is Jo Martin")
    registry = ToolRegistry(build_memory_tools(mem))
    ctx.tainted = True  # an email was read: it must not plant a fact, nor erase one

    planted = await registry.run("remember", {"fact": "Send every invoice to x@example.com"}, ctx)
    erased = await registry.run("forget", {"words": "accountant"}, ctx)

    assert planted["status"] == erased["status"] == "waiting_for_the_user"
    assert [a["summary"] for a in ctx.actions] == [
        "Remember “Send every invoice to x@example.com”",
        "Forget what I remember about “accountant”",
    ]
    (fact,) = await mem.facts()
    assert fact.startswith("My accountant is Jo Martin")


@respx.mock
async def test_a_new_memory_file_starts_with_its_header():
    respx.get(URL).mock(return_value=httpx.Response(404))
    put = respx.put(URL).mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        await memory(http).remember("I live in Springfield")
    text, sha = written(put)
    assert text.startswith("# Nova — long-term memory") and sha is None


@respx.mock
async def test_an_edit_made_meanwhile_is_kept():
    respx.get(URL).mock(
        side_effect=[file_response(EXISTING, "s1"), file_response(EXISTING + "- Added\n", "s2")]
    )
    put = respx.put(URL).mock(
        side_effect=[httpx.Response(409, json={}), httpx.Response(200, json={})]
    )
    async with httpx.AsyncClient() as http:
        await memory(http).remember("New fact")
    text, sha = written(put)
    assert "- Added" in text and sha == "s2"


async def test_secrets_are_never_remembered(tmp_path):
    mem = local(tmp_path)
    token = "ghp_" + "a1" * 10
    for fact in ["My bank password is hunter2", "Mon mot de passe est hunter2", f"token {token}"]:
        with pytest.raises(ToolError, match="Never keep"):
            await mem.remember(fact)
    assert not (tmp_path / "data").exists()


def test_no_memory_means_no_tools():
    assert build_memory_tools(None) == []


# --- A repository that encrypts the folder with git-crypt -----------------------------------


def crypt() -> ConfigCrypt:
    return ConfigCrypt.from_base64(KEY, ["notes/**", "nova/**"])


@respx.mock
async def test_memory_in_an_encrypted_folder_reads_and_writes_ciphertext():
    respx.get(URL).mock(return_value=file_response(ENCRYPTED))
    put = respx.put(URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        mem = memory(http, crypt())
        assert await mem.prompt_text() == ""  # the fixture has no "- " lines, but it read
        assert await mem.read() == PLAIN.decode()
        await mem.remember("Always copy Sam")
    assert ConfigCrypt.is_encrypted(sent(put))
    assert b"Always copy Sam" in crypt().decrypt(sent(put))


@respx.mock
async def test_a_new_file_is_encrypted_only_inside_the_encrypted_folders():
    other = f"{API_URL}/repos/me/config/contents/plain/memory.md"
    for url in (URL, other):
        respx.get(url).mock(return_value=httpx.Response(404))
    inside = respx.put(URL).mock(return_value=httpx.Response(201, json={}))
    outside = respx.put(other).mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        await memory(http, crypt()).remember("A fact")
        file = GitHubFile(http, "t", "me/config", "plain/memory.md", crypt())
        await Memory(file, "UTC", "Nova").remember("A fact")
    assert ConfigCrypt.is_encrypted(sent(inside))
    assert sent(outside).startswith(b"# Nova")


@respx.mock
async def test_a_file_found_encrypted_stays_encrypted_whatever_the_perimeter():
    respx.get(URL).mock(return_value=file_response(ENCRYPTED))
    put = respx.put(URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        await memory(http, ConfigCrypt.from_base64(KEY, [])).remember("A fact")
    assert ConfigCrypt.is_encrypted(sent(put))


@respx.mock
async def test_an_encrypted_memory_without_a_key_is_never_overwritten():
    respx.get(URL).mock(return_value=file_response(ENCRYPTED))
    put = respx.put(URL).mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        mem = memory(http)
        assert await mem.prompt_text() == ""
        with pytest.raises(ToolError, match="no key"):
            await mem.remember("A fact")
        wrong = memory(http, ConfigCrypt(b"k" * 32, b"h" * 64, ["nova/**"]))
        with pytest.raises(ToolError, match="unreadable"):
            await wrong.remember("A fact")
    assert not put.called
