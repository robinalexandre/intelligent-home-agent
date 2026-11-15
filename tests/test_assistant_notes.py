import base64
import json
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from test_config_crypt import KEY

from intelligent_home_agent.config_crypt import ConfigCrypt
from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.tools import (
    LocalFolder,
    NotesRepo,
    ToolContext,
    ToolError,
    ToolRegistry,
    build_assistant_note_tools,
)
from intelligent_home_agent.tools.assistant_notes import slugify
from intelligent_home_agent.tools.github import API_URL

CONTENTS = f"{API_URL}/repos/me/config/contents"
TODAY = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d")


def tools(http, language="en", crypt=None):
    repo = NotesRepo(http, "ghp_test", "me/config", crypt=crypt, author="nova")
    return {t.name: t for t in build_assistant_note_tools(repo, "UTC", language)}


def written(route, n=0):
    body = json.loads(route.calls[n].request.content)
    return body["message"], base64.b64decode(body["content"]).decode()


def served(text):
    content = base64.b64encode(text.encode()).decode()
    return httpx.Response(200, json={"sha": "abc", "content": content})


@respx.mock
async def test_a_reminder_is_written_in_the_reminders_format(ctx):
    route = respx.put(f"{CONTENTS}/reminders/{TODAY}-call-the-plumber-about-the-quote.md").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["create_assistant_reminder"].handler(
            {
                "title": "Call the plumber about the quote",
                "on": "2026-10-23 09:00",
                "every": "friday",
                "where": "dev/shop",
                "machine": "work-laptop",
                "group": "Example Corp",
                "details": "They wait for the price of the payment module.",
            },
            ctx,
        )

    path = f"reminders/{TODAY}-call-the-plumber-about-the-quote.md"
    assert result == {"created": path}
    message, text = written(route)
    assert message == f"chore(sync): add {path} from nova"
    assert text.startswith(
        "---\ntitle: Call the plumber about the quote\non: 2026-10-23 09:00\nevery: friday\n"
        "where: dev/shop\nmachine: work-laptop\ngroup: Example Corp\ncreated: "
    )
    assert text.endswith("---\n\nThey wait for the price of the payment module.\n")
    assert "until:" not in text
    assert route.calls[0].request.headers["authorization"] == "Bearer ghp_test"


@respx.mock
async def test_a_handoff_is_written_in_the_handoff_format_and_never_overwrites(ctx):
    stem = f"{CONTENTS}/handoffs/{TODAY}-finish-the-checkout-page"
    taken = respx.put(f"{stem}.md").mock(return_value=httpx.Response(422, json={}))
    route = respx.put(f"{stem}-2.md").mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        result = await tools(http)["create_handoff"].handler(
            {
                "title": "Finish the checkout page",
                "repo": "me/shop",
                "goal": "Let a customer pay by card.",
                "next_steps": "- Test a payment\n- Publish the docs",
            },
            ctx,
        )

    assert taken.called
    assert result == {"created": f"handoffs/{TODAY}-finish-the-checkout-page-2.md"}
    _, text = written(route)
    assert "\nfrom: nova\n" in text
    assert "\nrepo: me/shop\nbranch: none\n---\n" in text
    assert "## Goal\n\nLet a customer pay by card.\n" in text
    assert "## Next steps\n\n- Test a payment\n- Publish the docs\n" in text
    assert "## Open questions" not in text


async def test_notes_refuse_secrets_and_bad_dates(ctx):
    async with httpx.AsyncClient() as http:
        reminder = tools(http)["create_assistant_reminder"]
        with pytest.raises(ToolError, match="key or token"):
            await reminder.handler(
                {"title": "Token", "on": "2026-10-23", "details": "ghp_abcdefghijklmnop1234"}, ctx
            )
        with pytest.raises(ToolError, match="YYYY-MM-DD"):
            await reminder.handler({"title": "x", "on": "tomorrow", "details": "y"}, ctx)


def test_every_write_waits_after_private_content():
    notes = tools(None)
    assert list(notes) == [
        "create_assistant_reminder",
        "create_handoff",
        "close_assistant_reminder",
        "delete_handoff",
        "delete_assistant_reminder",
    ]
    assert all(t.needs_clean_turn and t.parks for t in notes.values())
    for name in ("close_assistant_reminder", "delete_assistant_reminder", "delete_handoff"):
        assert notes[name].always_propose and notes[name].proposable
    assert notes["delete_assistant_reminder"].needs_user_request
    assert notes["delete_handoff"].needs_user_request
    assert build_assistant_note_tools(None, "UTC") == []


async def test_a_handoff_after_a_web_page_waits_for_the_users_go_shown_whole(ctx):
    registry = ToolRegistry(list(tools(None, "fr").values()))
    ctx.read_page("example.com")
    args = {
        "title": "Fewer refusals",
        "goal": "Make the checkout less strict",
        "next_steps": "1. The form\n2. The accents bug",
    }
    parked = await registry.run("create_handoff", args, ctx)
    assert parked["status"] == "waiting_for_the_user"
    summary = ctx.actions[0]["summary"]
    assert summary.startswith("Note de passation pour ta prochaine session de code — title:")
    assert "1. The form ⏎ 2. The accents bug" in summary
    clean = ToolContext(Conversation(id="d"))
    deleted = await registry.run("delete_handoff", {"id": "2026-11-12-x"}, clean)
    assert deleted["status"] == "waiting_for_the_user"
    assert clean.actions[0]["summary"] == (
        "Supprimer la note de passation handoffs/2026-11-12-x.md"
    )


@respx.mock
async def test_closing_a_one_off_reminder_moves_it_to_past(ctx):
    respx.get(f"{CONTENTS}/reminders/2026-10-24-a.md").mock(
        return_value=served("---\ntitle: Call the plumber\non: 2026-10-24\n---\n\nSoon.\n")
    )
    added = respx.put(f"{CONTENTS}/reminders/past/2026-10-24-a.md").mock(
        return_value=httpx.Response(201, json={})
    )
    deleted = respx.delete(f"{CONTENTS}/reminders/2026-10-24-a.md").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as http:
        tool = tools(http)["close_assistant_reminder"]
        result = await tool.handler({"id": "2026-10-24-a"}, ctx)
        shown = await tool.summary({"id": "2026-10-24-a"})
        shown_fr = await tools(http, "fr")["close_assistant_reminder"].summary(
            {"id": "2026-10-24-a", "stop": True}
        )

    assert result == {"closed": "reminders/past/2026-10-24-a.md"}
    assert "\nclosed: " in written(added)[1]
    assert json.loads(deleted.calls[0].request.content)["sha"] == "abc"
    assert shown == "Mark the assistant reminder “Call the plumber” (2026-10-24-a) done"
    assert shown_fr == "Arrêter pour de bon le rappel répété « Call the plumber » (2026-10-24-a)"


@respx.mock
async def test_closing_a_repeating_reminder_closes_it_for_today_unless_stopped(ctx):
    text = "---\ntitle: Timesheet\non: 2026-10-24\nevery: friday\n---\n\nFill it in.\n"
    respx.get(f"{CONTENTS}/reminders/ts.md").mock(return_value=served(text))
    updated = respx.put(f"{CONTENTS}/reminders/ts.md").mock(
        return_value=httpx.Response(200, json={})
    )
    moved = respx.put(f"{CONTENTS}/reminders/past/ts.md").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.delete(f"{CONTENTS}/reminders/ts.md").mock(return_value=httpx.Response(200, json={}))
    async with httpx.AsyncClient() as http:
        tool = tools(http)["close_assistant_reminder"]
        assert await tool.handler({"id": "ts"}, ctx) == {"closed_for_today": "ts"}
        message, new = written(updated)
        assert f"\ndone: {TODAY}\n" in new
        assert message == f"chore(sync): close reminders/ts.md for {TODAY} from nova"
        assert not moved.called
        assert await tool.handler({"id": "ts", "stop": True}, ctx) == {
            "closed": "reminders/past/ts.md"
        }


@respx.mock
async def test_deleting_a_reminder_finds_it_open_or_past(ctx):
    respx.get(f"{CONTENTS}/reminders/old.md").mock(return_value=httpx.Response(404))
    respx.get(f"{CONTENTS}/reminders/past/old.md").mock(return_value=served("---\n---\n"))
    deleted = respx.delete(f"{CONTENTS}/reminders/past/old.md").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{CONTENTS}/reminders/none.md").mock(return_value=httpx.Response(404))
    respx.get(f"{CONTENTS}/reminders/past/none.md").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        tool = tools(http)["delete_assistant_reminder"]
        assert await tool.handler({"id": "old"}, ctx) == {"deleted": "reminders/past/old.md"}
        with pytest.raises(ToolError, match="No reminder none"):
            await tool.handler({"id": "none"}, ctx)
        for bad in ("../handoffs/x", "a/b", ".."):
            with pytest.raises(ToolError, match="id is"):
                await tool.handler({"id": bad}, ctx)

    assert json.loads(deleted.calls[0].request.content)["sha"] == "abc"


@respx.mock
async def test_notes_in_an_encrypted_folder_are_written_and_read_as_git_crypt_does(ctx):
    crypt = ConfigCrypt.from_base64(KEY, ["reminders/**"])
    reminder = respx.put(f"{CONTENTS}/reminders/{TODAY}-pay-the-invoice.md").mock(
        return_value=httpx.Response(201, json={})
    )
    handoff = respx.put(f"{CONTENTS}/handoffs/{TODAY}-pay.md").mock(
        return_value=httpx.Response(201, json={})
    )
    text = "---\ntitle: Timesheet\nevery: day\n---\n\nFill it in.\n"
    stored = base64.b64encode(crypt.encrypt(text.encode())).decode()
    respx.get(f"{CONTENTS}/reminders/ts.md").mock(
        return_value=httpx.Response(200, json={"sha": "abc", "content": stored})
    )
    updated = respx.put(f"{CONTENTS}/reminders/ts.md").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as http:
        notes = tools(http, crypt=crypt)
        await notes["create_assistant_reminder"].handler(
            {"title": "Pay the invoice", "on": "2026-10-23", "details": "Before Friday."}, ctx
        )
        await notes["create_handoff"].handler({"title": "Pay", "goal": "g", "next_steps": "n"}, ctx)
        await notes["close_assistant_reminder"].handler({"id": "ts"}, ctx)
        # Without the key, an encrypted note is refused rather than read as noise.
        with pytest.raises(ToolError, match="encrypted"):
            await tools(http)["close_assistant_reminder"].handler({"id": "ts"}, ctx)

    def raw(route):
        return base64.b64decode(json.loads(route.calls[0].request.content)["content"])

    assert ConfigCrypt.is_encrypted(raw(reminder))
    assert b"Before Friday." in crypt.decrypt(raw(reminder))
    assert raw(handoff).startswith(b"---\ntitle: Pay\n")  # outside the encrypted folders
    assert f"\ndone: {TODAY}\n".encode() in crypt.decrypt(raw(updated))
    assert ConfigCrypt.is_encrypted(raw(updated))


async def test_a_local_folder_keeps_notes_as_files_the_user_can_edit(tmp_path):
    folder = LocalFolder(tmp_path / "tasks")
    assert await folder.named_files() == {}
    text = "---\ntitle: Call\non: 2026-10-23 18:00\n---\n\nRemind me.\n"
    name = await folder.create("Call the plumber", text, "2026-10-23")
    again = await folder.create("Call the plumber", text, "2026-10-23")
    assert (name, again) == ("2026-10-23-call-the-plumber.md", "2026-10-23-call-the-plumber-2.md")
    path = tmp_path / "tasks" / name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert await folder.named_files() == {name: text, again: text}

    await folder.close_for_day(name, "2026-10-24")
    assert "\ndone: 2026-10-24\n---" in await folder.text(name)
    assert await folder.close(name, "2026-10-24 18:01") == f"past/{name}"
    assert "\nclosed: 2026-10-24 18:01\n" in (tmp_path / "tasks" / "past" / name).read_text()
    assert list(await folder.named_files()) == [again]  # past ones are not listed

    assert await folder.delete(again) and not await folder.delete(again)
    with pytest.raises(ToolError, match="key or token"):
        await folder.put("x", "sk-ant-abcdefghijklmnop")
    for bad in ("../outside", "a/b", ""):
        with pytest.raises(ToolError):
            await folder.put(bad, "text")
    assert not (tmp_path / "outside.md").exists()


def test_slugify():
    assert slugify("Appointment at the optician's, 9 am!") == "appointment-at-the-optician-s-9-am"
    assert slugify("Rendez-vous à 9h") == "rendez-vous-a-9h"
    assert slugify("🤖") == "note"
