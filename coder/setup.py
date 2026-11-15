"""The coder's Claude Code settings, written at each start (idempotent).

- The PermissionRequest hook (coder/permission_hook.py) in the user settings, so every
  Remote Control session asks the user through the agent.
- The repositories' folder marked trusted: Remote Control refuses an untrusted folder, and
  nobody is at a terminal to accept it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/work/.claude"))
REPOS = Path(os.environ.get("CODER_REPOS", "/work/repos"))
HOOK = {
    "matcher": "*",
    "hooks": [
        {
            "type": "command",
            "command": "python /app/coder/permission_hook.py",
            "timeout": 300,
        }
    ],
}


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def settings(data: dict[str, Any]) -> dict[str, Any]:
    hooks = data.setdefault("hooks", {})
    others = [h for h in hooks.get("PermissionRequest", []) if h != HOOK and not _ours(h)]
    hooks["PermissionRequest"] = [*others, HOOK]
    return data


def _ours(entry: Any) -> bool:
    return isinstance(entry, dict) and any(
        "permission_hook.py" in str(h.get("command", "")) for h in entry.get("hooks", [])
    )


def trusted(data: dict[str, Any], folder: Path) -> dict[str, Any]:
    project = data.setdefault("projects", {}).setdefault(str(folder), {})
    project["hasTrustDialogAccepted"] = True
    return data


def main() -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    REPOS.mkdir(parents=True, exist_ok=True)
    _write(CONFIG / "settings.json", settings(_read(CONFIG / "settings.json")))
    _write(CONFIG / ".claude.json", trusted(_read(CONFIG / ".claude.json"), REPOS))


if __name__ == "__main__":
    main()
