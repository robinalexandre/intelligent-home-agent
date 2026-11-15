"""The coder's view of every Claude Code session, read from the transcripts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coder import app as coder
from coder import sessions

AUTH = {"Authorization": "Bearer coder-secret"}
NOW = 1_800_000_000.0


def user(text: Any, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def assistant(blocks: list[dict[str, Any]], stop: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": blocks, "stop_reason": stop},
        **extra,
    }


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


TOOL = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
RESULT = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]


def transcript(
    config: Path, session: str, entries: list[dict[str, Any]], mtime: float, folder: str = "f"
) -> Path:
    file = config / "projects" / folder / f"{session}.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True)
    base = {"cwd": "/work/repos/shop", "entrypoint": "remote_mobile", "sessionId": session}
    lines = [
        json.dumps({**base, **e} if e["type"] in ("user", "assistant") else e) for e in entries
    ]
    file.write_text("\n".join(lines) + "\n")
    os.utime(file, (mtime, mtime))
    return file


def test_a_finished_turn_waits_for_the_user_with_its_title_and_last_words(tmp_path):
    file = transcript(
        tmp_path,
        "s1",
        [
            user("Prepare the public repository"),
            {"type": "ai-title", "aiTitle": "Public repo", "sessionId": "s1"},
            assistant([TOOL], "tool_use"),
            user(RESULT),
            assistant([text("It is ready.")], "end_turn"),
            {"type": "custom-title", "customTitle": "Shop public repo preparation"},
        ],
        NOW - 30,
    )
    found = sessions.read(file, booted=NOW - 3600, now=NOW)
    assert found is not None
    assert found.title == "Shop public repo preparation"
    assert found.status == sessions.WAITING
    assert found.folder == "repos/shop"
    assert found.entrypoint == "remote_mobile"
    assert found.last_prompt == "Prepare the public repository"
    assert found.last_said == "It is ready."


@pytest.mark.parametrize(
    ("mtime", "booted", "status"),
    [
        (NOW - 10, NOW - 3600, sessions.RUNNING),
        (NOW - 3000, NOW - 3600, sessions.SILENT),
        (NOW - 3000, NOW - 60, sessions.CUT),
    ],
)
def test_a_turn_in_progress_is_running_silent_or_cut_by_a_restart(tmp_path, mtime, booted, status):
    file = transcript(
        tmp_path, "s2", [user("Lance les tests"), assistant([TOOL], "tool_use")], mtime
    )
    found = sessions.read(file, booted=booted, now=NOW)
    assert found is not None
    assert found.status == status
    assert found.title == "Lance les tests"


def test_an_interrupted_turn_waits_and_the_interrupt_is_not_a_prompt(tmp_path):
    file = transcript(
        tmp_path,
        "s3",
        [
            user("Fais X"),
            assistant([TOOL], "tool_use"),
            user([text("[Request interrupted by user]")]),
        ],
        NOW - 10,
    )
    found = sessions.read(file, booted=NOW - 3600, now=NOW)
    assert found is not None
    assert found.status == sessions.WAITING
    assert found.last_prompt == "Fais X"


def test_sidechains_and_empty_files_are_left_out(tmp_path):
    empty = transcript(tmp_path, "s4", [{"type": "summary", "summary": "x"}], NOW)
    assert sessions.read(empty, booted=0, now=NOW) is None
    file = transcript(
        tmp_path,
        "s5",
        [
            user("Vrai prompt"),
            assistant([text("Fini.")], "end_turn"),
            user("sous-agent", isSidechain=True),
        ],
        NOW,
    )
    found = sessions.read(file, booted=0, now=NOW)
    assert found is not None and found.status == sessions.WAITING


def test_a_big_transcript_is_read_by_its_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "HEAD_BYTES", 200)
    monkeypatch.setattr(sessions, "TAIL_BYTES", 400)
    filler = [assistant([text("bla " * 40)], "tool_use") for _ in range(30)]
    file = transcript(
        tmp_path,
        "s6",
        [user("Premier"), *filler, assistant([text("Dernier mot")], "end_turn")],
        NOW,
    )
    found = sessions.read(file, booted=0, now=NOW)
    assert found is not None
    assert found.title == "Premier"
    assert found.last_prompt == ""
    assert found.last_said == "Dernier mot"
    with file.open("a") as fh:
        fh.write(json.dumps({"type": "last-prompt", "lastPrompt": "Premier"}) + "\n")
    found = sessions.read(file, booted=0, now=NOW)
    assert found is not None and found.last_prompt == "Premier"
    assert found.status == sessions.WAITING


def test_the_list_keeps_recent_sessions_newest_first_and_names_the_jobs(tmp_path):
    transcript(tmp_path, "old", [user("a"), assistant([text("b")], "end_turn")], NOW - 99 * 3600)
    transcript(tmp_path, "s7", [user("c"), assistant([text("d")], "end_turn")], NOW - 60)
    transcript(tmp_path, "s8", [user("e"), assistant([TOOL], "tool_use")], NOW - 5, folder="g")
    found = sessions.sessions(
        NOW - 3600, NOW, within_seconds=48 * 3600, jobs={"s7": "ab12"}, config=tmp_path
    )
    assert [(s["id"], s["status"], s["job"]) for s in found] == [
        ("s8", sessions.RUNNING, ""),
        ("s7", sessions.WAITING, "ab12"),
    ]


def test_history_gives_the_last_exchanges_without_tool_calls(tmp_path):
    transcript(
        tmp_path,
        "s9",
        [
            user("One"),
            assistant([text("Answer one")], "end_turn"),
            user("Two"),
            assistant([text("Looking"), TOOL], "tool_use"),
            user(RESULT),
            assistant([text("Answer two")], "end_turn"),
        ],
        NOW,
    )
    assert sessions.history("s9", turns=1, config=tmp_path) == [
        {"who": "user", "text": "Two"},
        {"who": "claude", "text": "Looking\nAnswer two"},
    ]
    assert sessions.history("nope", config=tmp_path) is None


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(coder, "WORK", tmp_path)
    monkeypatch.setattr(coder, "TOKEN", "coder-secret")
    monkeypatch.setattr(sessions, "CONFIG", tmp_path / ".claude")
    monkeypatch.setattr(coder, "BOOTED", time.time() - 3600)
    if hasattr(coder.app.state, "jobs"):
        del coder.app.state.jobs
    with TestClient(coder.app) as test_client:
        yield test_client


def test_the_endpoints_list_sessions_and_one_history(client, tmp_path):
    transcript(
        tmp_path / ".claude",
        "abc-123",
        [user("Hello"), assistant([text("Hi there")], "end_turn")],
        time.time() - 10,
    )
    assert client.get("/sessions").status_code == 401
    data = client.get("/sessions", headers=AUTH).json()
    assert data["started_at"]
    assert [s["id"] for s in data["sessions"]] == ["abc-123"]
    assert data["sessions"][0]["status"] == "waiting"
    history = client.get("/sessions/abc-123", headers=AUTH).json()["history"]
    assert history[-1] == {"who": "claude", "text": "Hi there"}
    assert client.get("/sessions/missing", headers=AUTH).status_code == 404
    assert client.get("/sessions/a%20b", headers=AUTH).status_code == 400


def inbox(config: Path, pid: int, session_id: str, socket: Path) -> None:
    folder = config / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    socket.touch()
    record = {"pid": pid, "sessionId": session_id, "messagingSocketPath": str(socket)}
    (folder / f"{pid}.json").write_text(json.dumps(record))


def test_inboxes_are_the_live_sessions_with_a_socket(tmp_path):
    inbox(tmp_path, os.getpid(), "live", tmp_path / "1.sock")
    inbox(tmp_path, 2**22 + 12345, "gone", tmp_path / "2.sock")
    (tmp_path / "sessions" / "bad.json").write_text("{")
    assert sessions.inboxes(tmp_path) == {"live": str(tmp_path / "1.sock")}


def test_a_send_message_result_counts_only_when_it_says_success():
    ok = [{"type": "text", "text": '{"success":true,"message":"in the inbox"}'}]
    assert coder._succeeded(ok)
    assert coder._succeeded('{"success": true}')
    assert not coder._succeeded([{"type": "text", "text": '{"success":false}'}])
    assert not coder._succeeded("Error: no such session")


def test_a_message_goes_through_the_relay_to_a_live_session_only(client, tmp_path, monkeypatch):
    config = tmp_path / ".claude"
    transcript(
        config, "abc-123", [user("Hello"), assistant([text("Hi there")], "end_turn")], time.time()
    )
    calls: list[tuple[str, str]] = []
    outcome = [(True, "Delivered.")]

    async def relay(address: str, body: str) -> tuple[bool, str]:
        calls.append((address, body))
        return outcome[0]

    monkeypatch.setattr(coder, "run_relay", relay)
    post = {"json": {"text": "Go ahead"}, "headers": AUTH}
    assert client.post("/sessions/abc-123/messages", **post).status_code == 409
    assert client.get("/sessions", headers=AUTH).json()["sessions"][0]["reachable"] is False

    inbox(config, os.getpid(), "abc-123", tmp_path / "in.sock")
    assert client.get("/sessions", headers=AUTH).json()["sessions"][0]["reachable"] is True
    resp = client.post("/sessions/abc-123/messages", **post)
    assert resp.status_code == 200 and resp.json()["sent"] is True
    assert calls == [(f"uds:{tmp_path / 'in.sock'}", "Go ahead")]

    outcome[0] = (False, "SendMessage was not called.")
    resp = client.post("/sessions/abc-123/messages", **post)
    assert resp.status_code == 502 and "not called" in resp.json()["detail"]
    assert client.post("/sessions/abc-123/messages", json={"text": "x"}).status_code == 401


def test_the_turn_starts_with_the_users_last_prompt(tmp_path):
    file = transcript(
        tmp_path,
        "t1",
        [
            user("One", timestamp="2026-11-15T10:00:00.120Z"),
            assistant([text("Ok")], "end_turn", timestamp="2026-11-15T10:00:09.000Z"),
            user("Two", timestamp="2026-11-15T10:05:00.500Z"),
            assistant([TOOL], "tool_use", timestamp="2026-11-15T10:05:02.000Z"),
            user(RESULT, timestamp="2026-11-15T10:06:00.000Z"),
        ],
        NOW,
    )
    found = sessions.read(file, booted=0, now=NOW)
    assert found is not None and found.turn_started == "2026-11-15T10:05:00Z"
