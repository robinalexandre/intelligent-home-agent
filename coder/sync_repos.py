"""Every repository the GitHub token reaches, cloned under /work/repos and kept fresh.

Like the user's own computer: their repositories and their organisations', one folder each
(by name; "owner-name" when two owners share a name). A clone with local work is only
fetched; a clean one on its default branch is fast-forwarded. Archived repositories are
left out. Run at start and then every SYNC_SECONDS, only with CODER_SYNC_REPOS=true
(coder/entrypoint.sh): it is for Remote Control, the jobs clone what they work on.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("coder.repos")

REPOS = Path(os.environ.get("CODER_REPOS", "/work/repos"))
SYNC_SECONDS = int(os.environ.get("CODER_SYNC_SECONDS", "1800"))


def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600, check=False
    )


def listed() -> list[dict[str, str]]:
    out = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "user/repos?per_page=100&affiliation=owner,collaborator,organization_member",
            "--jq",
            ".[] | select(.archived | not) | {full: .full_name, branch: .default_branch}",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if out.returncode:
        log.warning("Could not list the repositories: %s", out.stderr.strip()[:300])
        return []
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def folders(repos: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    names = [r["full"].split("/", 1)[1] for r in repos]
    shared = {n for n in names if names.count(n) > 1}
    chosen = {}
    for repo in repos:
        owner, name = repo["full"].split("/", 1)
        chosen[f"{owner}-{name}" if name in shared else name] = repo
    return chosen


def refresh(folder: Path, branch: str) -> None:
    git("fetch", "--quiet", "--prune", cwd=folder)
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=folder).stdout.strip()
    dirty = git("status", "--porcelain", cwd=folder).stdout.strip()
    if current == branch and not dirty:
        git("merge", "--ff-only", "--quiet", f"origin/{branch}", cwd=folder)


def sync_once() -> None:
    REPOS.mkdir(parents=True, exist_ok=True)
    for name, repo in folders(listed()).items():
        # A folder like .claude in the trusted root would hand Claude Code settings or hooks.
        if name.startswith("."):
            continue
        folder = REPOS / name
        if (folder / ".git").exists():
            refresh(folder, repo["branch"])
            continue
        done = git("clone", "--quiet", f"https://github.com/{repo['full']}.git", str(folder))
        if done.returncode:
            log.warning("Could not clone %s: %s", repo["full"], done.stderr.strip()[:300])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    while True:
        try:
            sync_once()
        except Exception:
            log.exception("Repository sync failed")
        if "--loop" not in sys.argv:
            return
        time.sleep(SYNC_SECONDS)


if __name__ == "__main__":
    main()
