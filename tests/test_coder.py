"""The coder service's HTTP layer, with Claude Code replaced by a scripted turn."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coder import app as coder

AUTH = {"Authorization": "Bearer coder-secret"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(coder, "WORK", tmp_path)
    monkeypatch.setattr(coder, "TOKEN", "coder-secret")

    async def clone(repo: str, branch: str, into: Path) -> Path:
        target = into / repo.rsplit("/", 1)[-1]
        target.mkdir(parents=True)
        (target / "CLONED").write_text(f"{repo}@{branch}")
        return target

    monkeypatch.setattr(coder, "clone", clone)
    if hasattr(coder.app.state, "jobs"):
        del coder.app.state.jobs
    with TestClient(coder.app) as test_client:
        yield test_client


def wait_until(client: TestClient, job: str, status: str, timeout: float = 2) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/jobs/{job}/events", headers=AUTH).json()
        if data["status"] == status:
            return data
        time.sleep(0.02)
    raise AssertionError(f"job {job} never became {status}: {data}")


def scripted(*turns: list[dict[str, Any]]):
    """A run_turn that plays one scripted turn per call and records what it was asked."""
    calls: list[dict[str, Any]] = []
    script = list(turns)

    async def run_turn(prompt, cwd, resume, keep=None, *, model=""):
        calls.append({"prompt": prompt, "cwd": cwd, "resume": resume, "model": model})
        for item in script.pop(0):
            yield item

    run_turn.calls = calls  # type: ignore[attr-defined]
    return run_turn


def test_a_job_clones_the_repo_runs_a_turn_and_keeps_its_events(client, monkeypatch, tmp_path):
    run_turn = scripted(
        [
            {"kind": "session", "text": "sess-1"},
            {"kind": "tool", "text": "Bash"},
            {"kind": "said", "text": "PR: https://github.com/me/shop/pull/1"},
            {"kind": "result", "text": "done", "is_error": False},
        ]
    )
    monkeypatch.setattr(coder, "run_turn", run_turn)

    resp = client.post(
        "/jobs",
        json={
            "id": "ab12",
            "title": "Fix",
            "brief": "Fix it.",
            "repo": "me/shop",
            "branch": "dev",
            "model": "claude-sonnet-5-5",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200 and resp.json()["session"] == "ab12"
    data = wait_until(client, "ab12", "idle")

    assert run_turn.calls == [
        {
            "prompt": "Fix it.",
            "cwd": str(tmp_path / "ab12" / "shop"),
            "resume": "",
            "model": "claude-sonnet-5-5",
        }
    ]
    assert data["model"] == "claude-sonnet-5-5"
    assert (tmp_path / "ab12" / "shop" / "CLONED").read_text() == "me/shop@dev"
    assert [(e["n"], e["kind"], e["text"], e["status"]) for e in data["events"]] == [
        (1, "tool", "Bash", ""),
        (2, "said", "PR: https://github.com/me/shop/pull/1", ""),
        (3, "stopped", "", "idle"),
    ]
    assert data["session"] == "sess-1"
    assert client.get("/jobs/ab12/events?after=2", headers=AUTH).json()["events"][0]["n"] == 3
    saved = json.loads((tmp_path / "jobs" / "ab12.json").read_text())
    assert saved["session_id"] == "sess-1" and saved["status"] == "idle"
    assert saved["model"] == "claude-sonnet-5-5"
    assert (
        client.post(
            "/jobs", json={"id": "ab12", "title": "x", "brief": "y"}, headers=AUTH
        ).status_code
        == 409
    )


def test_a_follow_up_resumes_the_session_and_a_message_during_a_turn_waits(client, monkeypatch):
    gate = asyncio.Event()
    calls: list[dict[str, Any]] = []

    async def run_turn(prompt, cwd, resume, keep=None, *, model=""):
        calls.append({"prompt": prompt, "resume": resume})
        if len(calls) == 1:
            yield {"kind": "session", "text": "sess-1"}
            await gate.wait()
        yield {"kind": "said", "text": f"Answer to: {prompt}"}
        yield {"kind": "result", "text": "", "is_error": False}

    monkeypatch.setattr(coder, "run_turn", run_turn)
    client.post("/jobs", json={"id": "cd34", "title": "Fix", "brief": "Fix it."}, headers=AUTH)
    time.sleep(0.05)
    assert (
        client.post("/jobs/cd34/messages", json={"text": "And the cache?"}, headers=AUTH).json()[
            "status"
        ]
        == "running"
    )
    assert (
        client.post("/jobs/cd34/messages", json={"text": "With tests."}, headers=AUTH).status_code
        == 200
    )
    client.portal.call(gate.set)
    data = wait_until(client, "cd34", "idle")
    while len(calls) < 2 or data["events"][-1]["n"] < 4:
        time.sleep(0.02)
        data = client.get("/jobs/cd34/events", headers=AUTH).json()
        if data["status"] == "idle" and len(calls) == 2:
            break

    assert calls[1] == {"prompt": "And the cache?\n\nWith tests.", "resume": "sess-1"}
    kinds = [(e["kind"], e["text"]) for e in data["events"]]
    assert ("said", "Answer to: Fix it.") in kinds
    assert ("said", "Answer to: And the cache?\n\nWith tests.") in kinds
    assert kinds[-1] == ("stopped", "")

    # Once idle, a message starts a turn at once, resuming the session.
    client.post("/jobs/cd34/messages", json={"text": "Thanks."}, headers=AUTH)
    wait_until(client, "cd34", "idle")
    assert calls[2] == {"prompt": "Thanks.", "resume": "sess-1"}


def test_an_interrupt_ends_the_turn_and_drops_what_was_pending(client, monkeypatch):
    class FakeClient:
        def __init__(self) -> None:
            self.stop = asyncio.Event()

        async def interrupt(self) -> None:
            self.stop.set()

    async def run_turn(prompt, cwd, resume, keep=None, *, model=""):
        fake = FakeClient()
        keep(fake)
        yield {"kind": "said", "text": "Working."}
        await fake.stop.wait()
        yield {"kind": "result", "text": "", "is_error": False}

    monkeypatch.setattr(coder, "run_turn", run_turn)
    client.post("/jobs", json={"id": "ef56", "title": "Fix", "brief": "Fix it."}, headers=AUTH)
    time.sleep(0.05)
    client.post("/jobs/ef56/messages", json={"text": "later"}, headers=AUTH)
    assert client.post("/jobs/ef56/interrupt", headers=AUTH).status_code == 200
    data = wait_until(client, "ef56", "idle")
    assert [e["kind"] for e in data["events"]] == ["said", "stopped"]
    time.sleep(0.05)
    assert client.get("/jobs/ef56/events", headers=AUTH).json()["events"][-1]["n"] == 2


def test_a_failing_turn_is_reported(client, monkeypatch):
    async def run_turn(prompt, cwd, resume, keep=None, *, model=""):
        yield {"kind": "said", "text": "Hm."}
        raise RuntimeError("no network")

    monkeypatch.setattr(coder, "run_turn", run_turn)
    client.post("/jobs", json={"id": "aa11", "title": "Fix", "brief": "Fix it."}, headers=AUTH)
    data = wait_until(client, "aa11", "failed")
    assert data["events"][1]["text"] == "⚠️ The coder could not go on: no network"
    assert data["events"][2] == {**data["events"][2], "kind": "stopped", "status": "failed"}


def test_the_token_and_the_arguments_are_checked(client):
    assert client.post("/jobs", json={"id": "x", "title": "t", "brief": "b"}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/jobs/x/events", headers=bad).status_code == 401
    assert client.get("/jobs/zz/events", headers=AUTH).status_code == 404
    body = {"id": "ab12", "title": "t", "brief": "b", "repo": "shop"}
    assert client.post("/jobs", json=body, headers=AUTH).status_code == 400
    assert client.post("/jobs", json={**body, "id": "Bad Id"}, headers=AUTH).status_code == 422
    bad_model = {**body, "repo": "me/shop", "model": "opus; rm -rf"}
    assert client.post("/jobs", json=bad_model, headers=AUTH).status_code == 422
    assert client.get("/health").json() == {"status": "ok"}


def test_a_turn_running_when_the_service_stopped_is_closed_on_restart(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    state = coder.JobState("gh78", "Fix", status="running", cwd=str(tmp_path))
    state.add("said", "Coding.")
    (jobs_dir / "gh78.json").write_text(json.dumps(coder.asdict(state)))

    jobs = coder.Jobs(tmp_path)
    job = jobs.get("gh78")
    assert job.status == "idle"
    assert [(e["kind"], e["status"]) for e in job.events] == [("said", ""), ("stopped", "idle")]
    assert "restarted" in job.events[1]["text"]


def test_house_rules_are_the_body_of_the_agent_file(tmp_path):
    assert coder.house_rules(
        Path(__file__).resolve().parents[1] / "agents" / "coder.md"
    ).startswith("You are a software engineer")
    assert coder.house_rules(tmp_path / "missing.md") == ""
    plain = tmp_path / "plain.md"
    plain.write_text("Just rules.\n")
    assert coder.house_rules(plain) == "Just rules."


def test_the_coder_says_its_own_words_in_the_users_language(monkeypatch, tmp_path):
    monkeypatch.setattr(coder, "SAYS", coder.TEXTS["fr"])
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    state = coder.JobState("ij90", "Fix", status="running", cwd=str(tmp_path))
    (jobs_dir / "ij90.json").write_text(json.dumps(coder.asdict(state)))
    assert "redémarré" in coder.Jobs(tmp_path).get("ij90").events[0]["text"]
    assert set(coder.TEXTS["fr"]) == set(coder.TEXTS["en"])
