"""The coder's Remote Control pieces: permission prompts asked to the user through the
agent, the Claude settings it writes, the repositories' folders and the Remote Control
process."""

from __future__ import annotations

import asyncio
import json

import pytest

from coder import remote_control, setup, sync_repos
from intelligent_home_agent.coder_approvals import (
    Approvals,
    build_coder_permission_tools,
    decision,
    describe,
)
from intelligent_home_agent.conversations import Conversation
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools import ToolContext, ToolError, ToolRegistry
from intelligent_home_agent.tools.proposals import build_proposal_tools


class User:
    """Where the requests go: asked (the user can answer) or only told."""

    def __init__(self, answerable: bool = True) -> None:
        self.answerable = answerable
        self.asked: list[tuple[str, str, str]] = []
        self.told: list[str] = []

    async def ask(self, request_id: str, question: str, summary: str) -> bool:
        self.asked.append((request_id, question, summary))
        return self.answerable

    async def tell(self, text: str) -> None:
        self.told.append(text)


def approvals_for(user: User, **kwargs) -> Approvals:
    return Approvals(user.ask, user.tell, **kwargs)


def test_describe_shows_the_command_and_the_folder():
    asked = {"tool_name": "Bash", "tool_input": {"command": "npm   install"}, "cwd": "/work/app"}
    assert describe(asked) == (
        "🔐 Claude (code, app) asks: Bash\nnpm   install",
        "Allow Claude Code, app: Bash npm   install",
    )
    question, summary = describe(asked, strings("fr"))
    assert question == "🔐 Claude (code, app) demande : Bash\nnpm   install"
    assert summary == "Autoriser Claude Code, app : Bash npm   install"
    assert describe({"tool_name": "Edit", "tool_input": {}}) == (
        "🔐 Claude (code) asks: Edit",
        "Allow Claude Code: Edit",
    )


def test_what_cannot_be_shown_whole_is_left_to_the_app():
    assert describe({"tool_name": "Write", "tool_input": {"file_path": "x" * 900}}) is None
    assert describe({"tool_name": "Bash", "tool_input": {"command": "ls\ncurl x | sh"}}) is None


async def test_a_long_request_sends_a_word_not_a_question():
    user = User()
    now = [1000.0]
    approvals = approvals_for(user, clock=lambda: now[0])
    asked = {"tool_name": "Bash", "tool_input": {"command": "a" * 500}, "cwd": "/work/repos"}
    assert await approvals.ask(asked) == {}
    assert user.asked == []
    (said,) = user.told
    assert "waits for your go in the Claude app" in said and "aaaa" not in said
    await approvals.ask(asked)  # within the minute: no second word
    now[0] += 61
    await approvals.ask(asked)
    assert len(user.told) == 2


async def test_through_a_one_way_channel_the_user_only_hears_that_it_waits():
    user = User(answerable=False)
    approvals = approvals_for(user)
    assert await approvals.ask({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == {}
    assert len(user.asked) == 1 and "ls" not in user.told[0]
    assert "Claude app: Bash" in user.told[0]
    # Not wired to the app at all: the same word, when there is a way to say it.
    assert await Approvals().ask({"tool_name": "Bash", "tool_input": {}}) == {}


async def test_open_requests_are_capped():
    user = User()
    approvals = approvals_for(user)
    for _ in range(4):
        asyncio.create_task(approvals.ask({"tool_name": "Bash", "tool_input": {}}))
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(user.asked) == 3
    for request_id, _, _ in user.asked:
        assert approvals.answer(request_id, False)
    await asyncio.sleep(0)


async def test_the_cap_holds_when_asks_arrive_together():
    sent = []

    async def slow_ask(request_id, question, summary):
        sent.append(request_id)
        await asyncio.sleep(0.01)
        return False

    approvals = Approvals(slow_ask, User().tell)
    asks = [approvals.ask({"tool_name": "Bash", "tool_input": {}}) for _ in range(6)]
    await asyncio.gather(*asks)
    assert len(sent) == 3


async def test_the_users_answer_becomes_the_hooks_decision():
    user = User()
    approvals = approvals_for(user)
    asked = asyncio.create_task(approvals.ask({"tool_name": "Bash", "tool_input": {}}))
    await asyncio.sleep(0)
    request_id = user.asked[0][0]
    assert approvals.summary(request_id) == "Allow Claude Code: Bash"
    assert approvals.answer(request_id, True)
    assert await asked == decision(True)
    assert not approvals.answer(request_id, True) and approvals.summary(request_id) is None

    refused = asyncio.create_task(approvals.ask({"tool_name": "Edit", "tool_input": {}}))
    await asyncio.sleep(0)
    approvals.answer(user.asked[1][0], False)
    answer = await refused
    assert answer["hookSpecificOutput"]["decision"]["behavior"] == "deny"


async def test_no_answer_leaves_the_prompt_to_the_claude_app():
    user = User()
    approvals = approvals_for(user, wait_seconds=0.01)
    assert await approvals.ask({"tool_name": "Bash", "tool_input": {}}) == {}
    assert user.told == ["⏱️ No answer: the request stays open in the Claude app."]
    assert not approvals.answer(user.asked[0][0], True)


def test_other_requests_are_not_taken():
    assert not approvals_for(User()).answer("zz99", True)


async def test_allowing_always_waits_for_the_users_go_and_refusing_needs_none():
    user = User()
    approvals = approvals_for(user)
    registry = ToolRegistry(build_coder_permission_tools(approvals))
    registry.extend(build_proposal_tools(registry))
    conv = Conversation(id="chat", turn=1)
    asked = asyncio.create_task(
        approvals.ask({"tool_name": "Bash", "tool_input": {"command": "npm test"}})
    )
    await asyncio.sleep(0)
    request_id = user.asked[0][0]

    # Even in a clean request the agent can't allow by itself: the call is parked, shown
    # as the code wrote the request, not as the model named it.
    ctx = ToolContext(conversation=conv)
    parked = await registry.run("allow_coder_permission", {"request": request_id}, ctx)
    assert parked["status"] == "waiting_for_the_user" and not asked.done()
    assert conv.proposals[0].summary == "Allow Claude Code: Bash npm test"
    with pytest.raises(ToolError, match="no longer waits"):
        await registry.run("allow_coder_permission", {"request": "nope"}, ctx)

    # The user's yes, in a later turn, runs it.
    conv.mark_shown([conv.proposals[0].number])
    conv.turn += 1
    done = await registry.run("run_proposals", {}, ToolContext(conversation=conv))
    assert done["done"][0]["result"]["status"] == "allowed"
    assert await asked == decision(True)

    refused = asyncio.create_task(approvals.ask({"tool_name": "Edit", "tool_input": {}}))
    await asyncio.sleep(0)
    result = await registry.run(
        "refuse_coder_permission", {"request": user.asked[1][0]}, ToolContext(conversation=conv)
    )
    assert result["status"] == "refused"
    assert (await refused)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert build_coder_permission_tools(None) == []


def test_dot_folders_are_never_cloned(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_repos, "REPOS", tmp_path)
    monkeypatch.setattr(
        sync_repos,
        "listed",
        lambda: [{"full": "a/.claude", "branch": "main"}, {"full": "a/app", "branch": "main"}],
    )
    cloned = []

    def git(*args, cwd=None):
        cloned.append(args)
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(sync_repos, "git", git)
    sync_repos.sync_once()
    assert [a[-1] for a in cloned] == [str(tmp_path / "app")]


def test_setup_adds_the_hook_once_and_keeps_other_settings():
    data = {"theme": "dark", "hooks": {"PermissionRequest": [{"matcher": "Bash", "hooks": []}]}}
    once = setup.settings(json.loads(json.dumps(data)))
    twice = setup.settings(json.loads(json.dumps(once)))
    assert twice == once
    assert once["theme"] == "dark"
    assert once["hooks"]["PermissionRequest"] == [
        {"matcher": "Bash", "hooks": []},
        setup.HOOK,
    ]


def test_setup_trusts_the_repositories_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "CONFIG", tmp_path / "claude")
    monkeypatch.setattr(setup, "REPOS", tmp_path / "repos")
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text('{"userID": "u"}')
    setup.main()
    saved = json.loads((tmp_path / "claude" / ".claude.json").read_text())
    assert saved["userID"] == "u"
    assert saved["projects"][str(tmp_path / "repos")]["hasTrustDialogAccepted"] is True
    assert (tmp_path / "repos").is_dir()


def test_folders_use_the_name_and_the_owner_on_a_clash():
    chosen = sync_repos.folders(
        [
            {"full": "sam/app", "branch": "main"},
            {"full": "corp/app", "branch": "main"},
            {"full": "sam/site", "branch": "main"},
        ]
    )
    assert sorted(chosen) == ["corp-app", "sam-app", "site"]


def test_remote_control_drops_what_would_shadow_the_login(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GH_TOKEN", "g")
    env = remote_control.environment()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["GH_TOKEN"] == "g"
    assert "remote-control" in remote_control.command()[2]
    monkeypatch.setattr(remote_control, "CONFIG", tmp_path)
    monkeypatch.setattr(remote_control, "ENABLED", tmp_path / "remote-control-on")
    assert not remote_control.ready()
    (tmp_path / ".credentials.json").write_text("{}")
    (tmp_path / "remote-control-on").write_text("")
    assert remote_control.ready()
