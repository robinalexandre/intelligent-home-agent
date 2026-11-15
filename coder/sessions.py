"""Every Claude Code session of the coder, read from its transcripts.

The jobs are not the only sessions here: Remote Control opens one for each conversation the
user starts from claude.ai/code or the Claude app. They all write their transcript to
CLAUDE_CONFIG_DIR/projects/<folder>/<session>.jsonl, one JSON entry per line, so reading
those files tells, for any session, what it is about, what it last said and whether its
turn is over, without being the one that started it.

The state comes from the last entry of the conversation and the file's last write:

- ``waiting``: the turn ended (Claude's last message closed it); the session waits for the
  user.
- ``running``: mid-turn, written to in the last few minutes.
- ``silent``: mid-turn, but nothing written for a while: a long command, or a permission
  prompt waiting in the Claude app.
- ``cut``: mid-turn when the container stopped: the restart ended the turn.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/work/.claude"))
WAITING, RUNNING, SILENT, CUT = "waiting", "running", "silent", "cut"
# Mid-turn and written to within this long: still working.
RUNNING_SECONDS = 5 * 60
# Only the start and the end of a big transcript are read.
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024
MAX_TEXT = 1500
MAX_PROMPT = 300
# What closes a turn on Claude's side.
TURN_ENDS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}
# What Claude Code writes as the user's turn when they stop one: the turn is over.
INTERRUPTED = "[Request interrupted"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Session:
    id: str
    title: str
    folder: str
    status: str
    last_activity: str
    # The job this session runs, "" for one the user opened (Remote Control).
    job: str = ""
    # Where the user drives it from: "remote_mobile", "remote", "sdk-py"…
    entrypoint: str = ""
    last_prompt: str = ""
    last_said: str = ""
    # Its process is alive and has an inbox: ask_coder_session can write to it.
    reachable: bool = False
    # When the user's last prompt came in: how long the turn has run, or ran.
    turn_started: str = ""


def _entries(file: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The first and the last entries of a transcript; the same list twice when it is small."""
    size = file.stat().st_size
    with file.open("rb") as fh:
        if size <= HEAD_BYTES + TAIL_BYTES:
            entries = _parse(fh.read())
            return entries, entries
        head = fh.read(HEAD_BYTES)
        fh.seek(size - TAIL_BYTES)
        tail = fh.read()
    # A cut line at either edge doesn't parse and is skipped.
    return _parse(head), _parse(tail)


def _parse(raw: bytes) -> list[dict[str, Any]]:
    out = []
    for line in raw.decode(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _content(entry: dict[str, Any]) -> Any:
    message = entry.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _prompt(entry: dict[str, Any]) -> str:
    """What the user typed, for a user entry that is a prompt (not a tool's result)."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return ""
    content = _content(entry)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        text = " ".join(
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return ""
    text = " ".join(text.split())
    # Claude Code's own wrappers (commands, caveats, interrupts), not the user's words.
    if text.startswith(INTERRUPTED) or (text.startswith("<") and not text.startswith("<!")):
        return ""
    return text


def _interrupted(entry: dict[str, Any]) -> bool:
    content = _content(entry)
    if isinstance(content, list):
        content = " ".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict) and "text" in b
        )
    return isinstance(content, str) and content.lstrip().startswith(INTERRUPTED)


def _said(entry: dict[str, Any]) -> str:
    content = _content(entry)
    if not isinstance(content, list):
        return ""
    texts = [
        str(b.get("text") or "").strip()
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def read(
    file: Path, booted: float, now: float, jobs: dict[str, str] | None = None
) -> Session | None:
    """One transcript as a Session; None when it holds no conversation."""
    try:
        head, tail = _entries(file)
        mtime = file.stat().st_mtime
    except OSError:
        return None
    titles = {"custom-title": "", "ai-title": "", "summary": ""}
    first_prompt = last_prompt = last_said = cwd = entrypoint = turn_started = ""
    last: dict[str, Any] | None = None
    for entry in [*head, *tail] if head is not tail else head:
        kind = entry.get("type")
        if kind == "custom-title" and entry.get("customTitle"):
            titles["custom-title"] = str(entry["customTitle"])
        elif kind == "ai-title" and entry.get("aiTitle"):
            titles["ai-title"] = str(entry["aiTitle"])
        elif kind == "summary" and entry.get("summary"):
            titles["summary"] = str(entry["summary"])
    marked_prompt = ""
    for entry in tail:
        kind = entry.get("type")
        if kind == "last-prompt" and entry.get("lastPrompt"):
            marked_prompt = " ".join(str(entry["lastPrompt"]).split())
        if kind not in ("user", "assistant") or entry.get("isSidechain"):
            continue
        last = entry
        cwd = str(entry.get("cwd") or cwd)
        entrypoint = str(entry.get("entrypoint") or entrypoint)
        if prompt := _prompt(entry):
            last_prompt = prompt
            turn_started = _stamp(entry.get("timestamp"))
        if kind == "assistant" and (said := _said(entry)):
            last_said = said
    for entry in head:
        if prompt := _prompt(entry):
            first_prompt = prompt
            break
    if last is None:
        return None
    # The prompt itself may lie before the tail read; Claude Code also notes it apart.
    last_prompt = last_prompt or marked_prompt
    message = last.get("message") if isinstance(last.get("message"), dict) else {}
    ended = (last.get("type") == "assistant" and message.get("stop_reason") in TURN_ENDS) or (
        last.get("type") == "user" and _interrupted(last)
    )
    if ended:
        status = WAITING
    elif mtime < booted:
        status = CUT
    elif now - mtime <= RUNNING_SECONDS:
        status = RUNNING
    else:
        status = SILENT
    session_id = file.stem
    title = (
        titles["custom-title"]
        or titles["ai-title"]
        or titles["summary"]
        or _clip(first_prompt, 80)
        or session_id[:8]
    )
    return Session(
        id=session_id,
        title=title,
        folder=_folder(cwd),
        status=status,
        last_activity=iso(mtime),
        job=(jobs or {}).get(session_id, ""),
        entrypoint=entrypoint,
        last_prompt=_clip(last_prompt, MAX_PROMPT),
        last_said=_clip(last_said, MAX_TEXT),
        turn_started=turn_started,
    )


def _stamp(value: Any) -> str:
    """A transcript's timestamp ("2026-11-15T14:34:55.123Z") to the second, "" when absent."""
    text = str(value or "")
    return f"{text[:19]}Z" if len(text) >= 19 and text[10] == "T" else ""


def _folder(cwd: str) -> str:
    work = os.environ.get("CODER_WORK", "/work").rstrip("/") + "/"
    return cwd[len(work) :] if cwd.startswith(work) else cwd


def transcripts(config: Path | None = None) -> list[Path]:
    # Subagents write theirs in a folder of their own, below the session's.
    return list(((config or CONFIG) / "projects").glob("*/*.jsonl"))


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def inboxes(config: Path | None = None) -> dict[str, str]:
    """The inbox socket of each live session, by session id. Claude Code keeps a record of
    every running session in CLAUDE_CONFIG_DIR/sessions/<pid>.json, with its id and the
    socket other sessions message it through (cross-session messaging); a record whose
    process is gone is left out."""
    found: dict[str, str] = {}
    for file in ((config or CONFIG) / "sessions").glob("*.json"):
        try:
            data = json.loads(file.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid, socket = data.get("sessionId"), data.get("messagingSocketPath")
        if sid and socket and _alive(data.get("pid")) and Path(str(socket)).exists():
            found[str(sid)] = str(socket)
    return found


def sessions(
    booted: float,
    now: float,
    *,
    within_seconds: float,
    jobs: dict[str, str] | None = None,
    config: Path | None = None,
) -> list[dict[str, Any]]:
    """The sessions written to within ``within_seconds``, the latest first."""
    found = []
    reachable = inboxes(config)
    for file in transcripts(config):
        try:
            if now - file.stat().st_mtime > within_seconds:
                continue
        except OSError:
            continue
        if (session := read(file, booted, now, jobs)) is not None:
            session.reachable = session.id in reachable
            found.append(session)
    found.sort(key=lambda s: s.last_activity, reverse=True)
    return [asdict(s) for s in found]


def history(
    session_id: str, *, turns: int = 6, config: Path | None = None
) -> list[dict[str, str]] | None:
    """The last ``turns`` exchanges of one session: what the user asked and what Claude
    answered, tool calls left out. None when there is no such session."""
    files = [f for f in transcripts(config) if f.stem == session_id]
    if not files:
        return None
    _head, tail = _entries(files[0])
    out: list[dict[str, str]] = []
    for entry in tail:
        if entry.get("isSidechain"):
            continue
        if prompt := _prompt(entry):
            out.append({"who": "user", "text": _clip(prompt, MAX_TEXT)})
        elif entry.get("type") == "assistant" and (said := _said(entry)):
            if out and out[-1]["who"] == "claude":
                out[-1]["text"] = _clip(f"{out[-1]['text']}\n{said}", MAX_TEXT)
            else:
                out.append({"who": "claude", "text": _clip(said, MAX_TEXT)})
    keep: list[dict[str, str]] = []
    users = 0
    for item in reversed(out):
        keep.append(item)
        if item["who"] == "user":
            users += 1
            if users >= turns:
                break
    return list(reversed(keep))
