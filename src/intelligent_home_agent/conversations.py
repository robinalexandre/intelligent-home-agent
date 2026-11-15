"""Short-lived, in-memory conversation state.

Voice interactions are bursty: a request, maybe a follow-up ("yes, lock it"), then
silence. We keep each conversation for a few minutes so follow-ups and confirmations
work, then forget it. A written chat can be answered hours later, so it gets its own,
longer lifetime.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass
class PendingAction:
    """A sensitive action awaiting the user's confirmation."""

    id: str
    domain: str
    service: str
    entity_ids: list[str]
    data: dict[str, Any]
    created_turn: int
    summary: str


PROPOSAL_DAYS = 14
MAX_OPEN_QUESTIONS = 10
MAX_CLEAN_REPLIES = 50


@dataclass
class Proposal:
    """A write parked in a request that read others' content, until the user's go."""

    number: int
    tool: str
    args: dict[str, Any]
    summary: str
    created_turn: int
    created_at: float
    # Only a write the code listed in a reply that went out can run.
    shown: bool = False
    # The kept files it names, by content hash: a file changed since can't be used.
    file_hashes: dict[str, str] = field(default_factory=dict)
    # The poll it is an option of, where a chat shows one, and that option's text.
    poll_id: str = ""
    option: str = ""


@dataclass(frozen=True)
class Attachment:
    """A file the user sent, kept by name so it can be attached to an email draft.

    The name comes from whoever made the file: it is reduced to a safe character set, as
    it goes into the prompt and the history.
    """

    filename: str
    mimetype: str
    data: bytes = field(repr=False)
    # "user" when the user sent it; anything else came from a tool (e.g. "mail", "agent").
    origin: str = "user"

    def __post_init__(self) -> None:
        object.__setattr__(self, "filename", safe_filename(self.filename))


def file_hash(file: Attachment) -> str:
    return hashlib.sha256(file.data).hexdigest()


def safe_filename(name: str, max_length: int = 100) -> str:
    # Composed first: an accent sent decomposed (NFD, as iPhones do) is a mark \w doesn't
    # match, so "Café.pdf" would become "Cafe_.pdf".
    name = unicodedata.normalize("NFC", name)
    cleaned = re.sub(r"[^\w .,()+-]", "_", name).strip(" .") or "file"
    if len(cleaned) <= max_length:
        return cleaned
    stem, dot, ext = cleaned.rpartition(".")
    if not dot or len(ext) > 10:
        return cleaned[:max_length]
    return f"{stem[: max_length - len(ext) - 1]}.{ext}"


class FileStore:
    """Files the user sent or the agent made, kept on disk for some days, so they don't
    have to be sent again. Contents are stored under a hash, never under their name."""

    def __init__(self, directory: Path, keep_days: float = 30, clock: Any = time.time) -> None:
        self._dir = directory
        self._keep_seconds = keep_days * 86400
        self._clock = clock

    @property
    def _index(self) -> Path:
        return self._dir / "index.json"

    def _read_index(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._index.read_text())
        except (OSError, ValueError):
            return {}

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        tmp = self._index.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(self._index)

    def put(self, file: Attachment) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        blob = hashlib.sha256(file.data).hexdigest()
        path = self._dir / blob
        if not path.exists():
            path.write_bytes(file.data)
            os.chmod(path, 0o600)
        index = self._read_index()
        index[file.filename] = {
            "mimetype": file.mimetype,
            "origin": file.origin,
            "blob": blob,
            "saved_at": self._clock(),
        }
        self._write_index(index)

    def prune(self) -> dict[str, dict[str, Any]]:
        """Delete the files past their days; the index of the ones still kept."""
        index = self._read_index()
        cutoff = self._clock() - self._keep_seconds
        kept = {name: meta for name, meta in index.items() if meta["saved_at"] >= cutoff}
        if len(kept) != len(index):
            self._write_index(kept)
            used = {meta["blob"] for meta in kept.values()}
            for meta in index.values():
                if meta["blob"] not in used:
                    (self._dir / meta["blob"]).unlink(missing_ok=True)
        return kept

    def clear(self) -> None:
        """Delete every file kept here."""
        shutil.rmtree(self._dir, ignore_errors=True)

    def load(self) -> list[Attachment]:
        """The files still within their days, after deleting the older ones."""
        kept = self.prune()
        files = []
        for name, meta in kept.items():
            path = self._dir / meta["blob"]
            if path.exists():
                files.append(Attachment(name, meta["mimetype"], path.read_bytes(), meta["origin"]))
        return files


def nfc(name: str) -> str:
    """One spelling per accented name: some phones and drives send them decomposed (NFD),
    the model writes them composed (NFC)."""
    return unicodedata.normalize("NFC", name)


class FileMap(dict):
    """The conversation's files by name, whatever the accents' Unicode form: a file sent
    as "Café.pdf" from a phone is found when the model asks for it."""

    def __setitem__(self, key: str, value: Attachment) -> None:
        super().__setitem__(nfc(key), value)

    def __getitem__(self, key: str) -> Attachment:
        return super().__getitem__(nfc(key))

    def __contains__(self, key: object) -> bool:
        return super().__contains__(nfc(key) if isinstance(key, str) else key)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(nfc(key))

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(nfc(key), default)

    def pop(self, key: str, *default: Any) -> Any:
        return super().pop(nfc(key), *default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return super().setdefault(nfc(key), default)


@dataclass
class Conversation:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    turn: int = 0
    pending: dict[str, PendingAction] = field(default_factory=dict)
    proposals: list[Proposal] = field(default_factory=list)
    # Questions asked in a chat's poll: {"question", "options", "poll_id"}.
    questions: list[dict[str, Any]] = field(default_factory=list)
    # Plans the user approved, each to run as its own request once the reply went out.
    plans: list[str] = field(default_factory=list)
    # The last replies of requests that read nothing of others', whitespace folded: a
    # quote of one holds only what the history already does.
    clean_replies: list[str] = field(default_factory=list)
    files: dict[str, Attachment] = field(default_factory=lambda: FileMap())
    # Tool results too long for one read, by id (tools/long_results.py): memory only.
    long_results: dict[str, Any] = field(default_factory=dict, repr=False)
    long_result_count: int = 0
    updated_at: float = field(default_factory=time.monotonic)
    ttl_seconds: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Where this conversation's files are kept across restarts, if anywhere.
    file_store: FileStore | None = field(default=None, repr=False)
    # Saved to disk after each request (a chat), so it survives a restart.
    kept: bool = False

    def attach_store(self, store: FileStore) -> None:
        """Keep this conversation's files in the store, and bring back the saved ones."""
        if self.file_store is None:
            self.file_store = store
            for file in store.load():
                self.files.setdefault(file.filename, file)

    def add_file(self, file: Attachment) -> None:
        self.files[file.filename] = file
        if self.file_store is not None:
            self.file_store.put(file)

    def keep_file(self, file: Attachment) -> Attachment:
        """Keep a fetched file by name, never replacing one that came from elsewhere, nor
        one written by others with other contents: two emails can both send an
        invoice.pdf, and the user approved one of them. The agent's own files (a form it
        fills again) replace their earlier version."""
        stem, dot, ext = file.filename.rpartition(".")
        if not dot:
            stem, ext = file.filename, ""
        name, n = file.filename, 1
        while name in self.files and (
            self.files[name].origin != file.origin
            or (file.origin != "agent" and self.files[name].data != file.data)
        ):
            label = file.origin if n == 1 else f"{file.origin} {n}"
            name = f"{stem} ({label}){dot}{ext}"
            n += 1
        file = replace(file, filename=name)
        self.add_file(file)
        return file

    def open_proposals(self) -> list[Proposal]:
        cutoff = time.time() - PROPOSAL_DAYS * 86400
        self.proposals = [p for p in self.proposals if p.created_at >= cutoff]
        return self.proposals

    def propose(self, tool: str, args: dict[str, Any], summary: str) -> tuple[Proposal, bool]:
        """The parked write, and False when the same one was already waiting."""
        for proposal in self.open_proposals():
            if proposal.tool == tool and proposal.args == args:
                return proposal, False
        number = max((p.number for p in self.proposals), default=0) + 1
        attachments = args.get("attachments")
        named = [args.get("file_name"), *(attachments if isinstance(attachments, list) else [])]
        hashes = {
            name: file_hash(self.files[name])
            for name in named
            if isinstance(name, str) and name in self.files
        }
        proposal = Proposal(number, tool, args, summary, self.turn, time.time(), False, hashes)
        self.proposals.append(proposal)
        return proposal, True

    def mark_shown(self, numbers: list[int]) -> None:
        for proposal in self.proposals:
            if proposal.number in numbers:
                proposal.shown = True

    def drop_unshown(self) -> None:
        """A turn that failed never showed its writes: they must not run on a later yes."""
        self.proposals = [p for p in self.proposals if p.shown]

    def note_clean_reply(self, reply: str) -> None:
        if folded := " ".join(reply.split()):
            self.clean_replies = [*self.clean_replies, folded][-MAX_CLEAN_REPLIES:]

    def said_cleanly(self, quote: str) -> bool:
        """Whether a quote (maybe cut short) is one of the clean replies."""
        folded = " ".join(quote.split())
        return bool(folded) and any(reply.startswith(folded) for reply in self.clean_replies)

    def add_pending(self, **kwargs: Any) -> PendingAction:
        action = PendingAction(id=uuid.uuid4().hex[:8], created_turn=self.turn, **kwargs)
        self.pending[action.id] = action
        return action

    def trim(self, max_messages: int) -> None:
        """Drop the oldest messages while keeping the history valid for the API.

        The history must start with a plain user message (not a tool_result), otherwise
        the Messages API rejects it.
        """
        if len(self.messages) <= max_messages:
            return
        msgs = self.messages[-max_messages:]
        while msgs and not _is_plain_user_message(msgs[0]):
            msgs.pop(0)
        self.messages = msgs


def _is_plain_user_message(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


class ConversationStore:
    def __init__(self, ttl_seconds: int, clock: Any = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, Conversation] = {}

    def get(self, conversation_id: str, ttl_seconds: float | None = None) -> Conversation:
        self._evict_expired()
        conv = self._items.get(conversation_id)
        if conv is None:
            conv = Conversation(id=conversation_id, updated_at=self._clock())
            self._items[conversation_id] = conv
        if ttl_seconds is not None:
            conv.ttl_seconds = ttl_seconds
        return conv

    def peek(self, conversation_id: str) -> Conversation | None:
        """The conversation when it is still there; never creates one."""
        self._evict_expired()
        return self._items.get(conversation_id)

    def touch(self, conv: Conversation) -> None:
        conv.updated_at = self._clock()

    def reset(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)

    def save(self, conv: Conversation, path: Path) -> None:
        """Keep a conversation's text and proposals across restarts (files are kept by
        their own store; pending home actions are not kept)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "id": conv.id,
                    "turn": conv.turn,
                    "messages": conv.messages,
                    "ttl_seconds": conv.ttl_seconds,
                    "proposals": [asdict(p) for p in conv.open_proposals()],
                    "questions": conv.questions[-MAX_OPEN_QUESTIONS:],
                    "clean_replies": conv.clean_replies,
                    "saved_at": time.time(),
                },
                ensure_ascii=False,
            )
        )
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def restore(self, path: Path) -> Conversation | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        age = time.time() - data["saved_at"]
        if age > (data.get("ttl_seconds") or self._ttl):
            return None
        conv = self.get(data["id"], data.get("ttl_seconds"))
        conv.messages, conv.turn = data["messages"], data["turn"]
        conv.proposals = [Proposal(**p) for p in data.get("proposals", [])]
        conv.questions = data.get("questions", [])
        conv.clean_replies = data.get("clean_replies", [])
        conv.updated_at = self._clock() - age
        conv.kept = True
        return conv

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [
            k for k, c in self._items.items() if now - c.updated_at > (c.ttl_seconds or self._ttl)
        ]
        for key in expired:
            del self._items[key]
