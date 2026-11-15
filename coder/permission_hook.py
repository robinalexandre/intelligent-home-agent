"""Claude Code's PermissionRequest hook in the coder: ask the user through the agent.

Claude Code pipes the request (tool, input, folder) on stdin; the agent asks the user
where it can (docs/jobs.md) and answers with the hook's decision, or {} to leave the
prompt to the Claude app (Remote Control shows it there too). Any failure decides
nothing, the same way.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000").rstrip("/")
# Below the hook's own timeout in the coder's Claude settings (coder/setup.py).
WAIT_SECONDS = 280


def main() -> None:
    token = os.environ.get("CODER_TOKEN", "")
    try:
        request = json.load(sys.stdin)
    except ValueError:
        return
    call = urllib.request.Request(
        f"{AGENT_URL}/coder/permission",
        data=json.dumps(request).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(call, timeout=WAIT_SECONDS) as resp:
            answer = json.load(resp)
    except (OSError, ValueError, urllib.error.URLError):
        return
    if isinstance(answer, dict) and answer:
        print(json.dumps(answer))


if __name__ == "__main__":
    main()
