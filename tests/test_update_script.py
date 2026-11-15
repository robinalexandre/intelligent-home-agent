"""scripts/update.sh: it pulls, rebuilds the stack, and rebuilds the coder only when what
it is made of changed. Docker is a stand-in script; git is real, on repositories in tmp."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# A stand-in for `docker compose`: logs each call, and answers config like compose does.
COMPOSE = """#!/bin/sh
here="$(dirname "$0")"
echo "$*" >> "$here/compose.log"
case "$*" in
  "config --services") cat "$here/services" ;;
  "config --hash coder") echo "coder $(cat "$here/coder.hash")" ;;
esac
"""
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com")

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *IDENTITY, *args], cwd=repo, check=True, capture_output=True)


class Stack:
    """A server's checkout of the repository, the place it pulls from, and a fake docker."""

    def __init__(self, tmp: Path) -> None:
        self.upstream = tmp / "upstream"
        self.server = tmp / "server"
        self.tools = tmp / "tools"
        self.tools.mkdir()
        (self.tools / "compose").write_text(COMPOSE)
        os.chmod(self.tools / "compose", 0o700)
        (self.tools / "services").write_text("agent\ncoder\nwaha\n")
        (self.tools / "coder.hash").write_text("h1")
        (self.upstream / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts" / "update.sh", self.upstream / "scripts" / "update.sh")
        for path in ("coder/app.py", "agents/coder.md", "src/agent.py"):
            (self.upstream / path).parent.mkdir(exist_ok=True)
            (self.upstream / path).write_text("v1")
        (self.upstream / ".gitignore").write_text(".coder-deployed\n")
        git(self.upstream, "init", "-q", "-b", "main")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "one")
        subprocess.run(
            ["git", "clone", "-q", str(self.upstream), str(self.server)],
            check=True,
            capture_output=True,
        )

    def publish(self, path: str, text: str) -> None:
        """A change lands upstream: the server has it only once it pulled."""
        (self.upstream / path).write_text(text)
        git(self.upstream, "commit", "-qam", path)

    def update(self, **env: str) -> list[str]:
        log = self.tools / "compose.log"
        log.unlink(missing_ok=True)
        subprocess.run(
            ["sh", "scripts/update.sh"],
            cwd=self.server,
            check=True,
            env={**os.environ, "UPDATE_COMPOSE": str(self.tools / "compose"), **env},
            capture_output=True,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return [line for line in calls if line.startswith("up")]


@pytest.fixture
def stack(tmp_path):
    return Stack(tmp_path)


def test_the_coder_is_rebuilt_first_then_only_when_it_changed(stack):
    assert stack.update() == ["up -d --build"]  # nothing recorded yet
    stack.publish("src/agent.py", "v2")
    assert stack.update() == ["up -d --build agent waha"]
    assert (stack.server / "src" / "agent.py").read_text() == "v2"  # it pulled
    stack.publish("coder/app.py", "v2")
    assert stack.update() == ["up -d --build"]
    assert stack.update() == ["up -d --build agent waha"]
    # Its house rules are part of its image too.
    stack.publish("agents/coder.md", "v2")
    assert stack.update() == ["up -d --build"]


def test_a_change_of_its_compose_settings_or_a_forced_update_rebuilds_it(stack):
    stack.update()
    (stack.tools / "coder.hash").write_text("h2")
    assert stack.update() == ["up -d --build"]
    assert stack.update() == ["up -d --build agent waha"]
    assert stack.update(UPDATE_CODER="always") == ["up -d --build"]


def test_without_a_coder_it_is_a_plain_update(stack):
    (stack.tools / "services").write_text("agent\ncaddy\n")
    assert stack.update() == ["up -d --build"]
    assert stack.update() == ["up -d --build"]
    assert not (stack.server / ".coder-deployed").exists()


def test_it_can_rebuild_what_is_checked_out_without_pulling(stack):
    stack.update()
    stack.publish("coder/app.py", "v2")
    assert stack.update(UPDATE_PULL="no") == ["up -d --build agent waha"]
    assert (stack.server / "coder" / "app.py").read_text() == "v1"


def test_the_script_the_pull_brought_is_the_one_that_runs(stack):
    stack.update()
    stack.publish("scripts/update.sh", "#!/bin/sh\necho new > ran-new\n")
    assert stack.update() == []
    assert (stack.server / "ran-new").exists()
