"""Claude Code's Remote Control server, always on, in the repositories' folder.

The user opens sessions from claude.ai/code or the Claude app; each one runs here, on their
subscription. It needs a full Claude login (`claude auth login`, kept on the /work volume):
the setup-token of the jobs can't open Remote Control and would shadow the login, so it is
taken out of this process's environment. It starts once the user has turned it on
(coder/setup-remote-control.sh writes ENABLED) and restarts when it exits (it gives up after
about ten minutes without network). A pseudo-terminal stands in for the terminal it expects.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("coder.remote_control")

CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/work/.claude"))
REPOS = Path(os.environ.get("CODER_REPOS", "/work/repos"))
ENABLED = CONFIG / "remote-control-on"
LOG = Path(os.environ.get("CODER_WORK", "/work")) / "remote-control.log"
NAME = os.environ.get("CODER_RC_NAME", "Home agent code")
PAUSE_SECONDS = 30
# Variables that outrank the login or switch Remote Control off.
DROPPED = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)


def command() -> list[str]:
    rc = f"claude remote-control --name {NAME!r} --spawn same-dir --permission-mode default"
    return ["script", "-qfec", rc, "/dev/null"]


def environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in DROPPED}


def ready() -> bool:
    return ENABLED.exists() and (CONFIG / ".credentials.json").exists()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    REPOS.mkdir(parents=True, exist_ok=True)
    while True:
        if ready():
            log.info("Starting Remote Control in %s", REPOS)
            with LOG.open("a") as out:
                code = subprocess.call(
                    command(),
                    cwd=REPOS,
                    env=environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            log.warning("Remote Control stopped (exit %s)", code)
        time.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    main()
