"""The user's mailboxes over IMAP: reading, plus drafts, flags and filing.

Any IMAP server works. What differs between providers is found from what the server says
rather than from its name: the special folders by their SPECIAL-USE marks (RFC 6154), then
by their usual names in a few languages, or by the account's own ``drafts``, ``sent`` and
``trash`` settings; the folder separator from LIST; Gmail's labels and All Mail from its
X-GM-EXT-1 capability.

Mailboxes are opened read-only and bodies fetched with BODY.PEEK, so reading never marks
a message as seen. Drafts are appended to the Drafts folder and nothing is ever sent: the
user reviews them in their mail app and sends them. A draft can be changed (saved anew,
the old one put in the trash) or put in the trash, always on the user's go. A received
email can be flagged, so the user sees which ones the agent acted on, or filed into one
of the user's own folders when they ask; none is ever deleted or put in the trash. The
emails the user sent can be searched and read, never flagged or filed.
"""

from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formatdate, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from intelligent_home_agent.config import MailAccount
from intelligent_home_agent.conversations import Attachment, safe_filename
from intelligent_home_agent.i18n import strings
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_MESSAGES_PER_ACCOUNT = 25
MAX_HOURS = 30 * 24
MAX_LIMIT = 100
# The furthest a page reaches back: each folder hands over that many emails.
MAX_LISTED = 200
MAX_SEARCH_DAYS = 10 * 365
SNIPPET_LENGTH = 300
# Longer bodies come in parts (tools/long_results.py).
MAX_BODY_CHARS = 150_000
# Enough for the headers and the start of the text part, without downloading attachments.
_PARTIAL_BYTES = 65536
_UID_RE = re.compile(rb"UID (\d+)")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_DRAFTS = 15
MAX_DRAFT_TEXT = 4000
# Smaller images are logos and signatures, not documents.
MIN_IMAGE_BYTES = 20 * 1024
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?:"(?P<delimiter>(?:[^"\\]|\\.)*)"|NIL) (?P<name>.+)')
# Folders an email is never filed into: moving there would hide or delete it.
_SPECIAL_USE = {"\\trash", "\\junk", "\\drafts", "\\sent", "\\all", "\\flagged", "\\noselect"}
# Servers that don't mark their special folders are recognised by the usual names, in the
# languages mail apps create them in. An account whose folders have other names says so
# with its drafts, sent and trash settings.
_SENT_NAMES = {
    "sent",
    "sent messages",
    "sent items",
    "sent mail",
    "envoyés",
    "messages envoyés",
    "éléments envoyés",
    "gesendet",
    "gesendete elemente",
    "gesendete objekte",
    "enviados",
    "elementos enviados",
    "inviati",
    "posta inviata",
    "verzonden",
    "verzonden items",
}
_TRASH_NAMES = {
    "trash",
    "bin",
    "deleted",
    "deleted messages",
    "deleted items",
    "corbeille",
    "éléments supprimés",
    "papierkorb",
    "gelöschte elemente",
    "papelera",
    "elementos eliminados",
    "cestino",
    "posta eliminata",
    "prullenbak",
    "verwijderde items",
}
_JUNK_NAMES = {
    "junk",
    "spam",
    "junk e-mail",
    "junk email",
    "bulk mail",
    "indésirables",
    "courrier indésirable",
    "pourriel",
    "spamverdacht",
    "junk-e-mail",
    "correo no deseado",
    "posta indesiderata",
    "ongewenste e-mail",
}
_DRAFT_NAMES = {"drafts", "draft", "brouillons", "entwürfe", "borradores", "bozze", "concepten"}
_SPECIAL_NAMES = _SENT_NAMES | _TRASH_NAMES | _JUNK_NAMES | _DRAFT_NAMES | {"inbox", "outbox"}
_KEPT_FLAGS = {"\\seen", "\\answered", "\\flagged"}
# Providers cap the open connections per account (Gmail refuses a login past 15, the
# phone's own included), and several requests can read the mail at once: a few each, and a
# short wait for one to close when the server says it is full.
MAX_SESSIONS_PER_ACCOUNT = 4
BUSY_RETRIES = 2
BUSY_WAIT_SECONDS = 5.0
_BUSY = re.compile(r"too many (?:simultaneous |concurrent )?connections", re.IGNORECASE)
_slots: dict[tuple[str, str], threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()
# Gmail search for All Mail: what is in no other searched folder, never sent or drafts.
_ALL_MAIL_FILTER = 'X-GM-RAW "-in:sent -in:drafts -in:chats -in:inbox -in:trash -in:spam"'


@dataclass(frozen=True)
class MailSummary:
    account: str
    sender: str
    subject: str
    received: datetime
    unread: bool
    snippet: str
    # The email's id for read_email: its IMAP UID, with its folder when not the inbox.
    uid: str = ""
    folder: str = ""
    deleted: bool = False
    recipients: str = ""
    sent: bool = False


class MailSource(Protocol):
    name: str

    async def recent(
        self, since: datetime, unread_only: bool, count: int = MAX_MESSAGES_PER_ACCOUNT
    ) -> list[MailSummary]: ...

    async def search(
        self,
        terms: dict[str, str],
        since: datetime,
        count: int,
        received: bool = True,
        sent: bool = True,
    ) -> list[MailSummary]: ...

    async def message(self, uid: str) -> dict[str, Any]: ...


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"style", "script", "head"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "head"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(parser.parts)


def _snippet(message: EmailMessage) -> str:
    try:
        body = message.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body is not None else ""
        if body is not None and body.get_content_subtype() == "html":
            text = _html_to_text(text)
    except (KeyError, LookupError, ValueError):
        text = ""
    return " ".join(text.split())[:SNIPPET_LENGTH]


def parse_message(raw: bytes, account: str, unread: bool) -> MailSummary | None:
    message = email.message_from_bytes(raw, policy=default_policy)
    try:
        received = parsedate_to_datetime(str(message["date"]))
    except (TypeError, ValueError):
        return None
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    return MailSummary(
        account=account,
        sender=str(message["from"] or ""),
        subject=str(message["subject"] or "(no subject)"),
        received=received,
        unread=unread,
        snippet=_snippet(message),  # type: ignore[arg-type]
        recipients=", ".join(str(message[h]) for h in ("to", "cc") if message[h]),
    )


def _attachment_file(part: Any) -> Attachment | None:
    try:
        data = part.get_payload(decode=True) or b""
    except (LookupError, ValueError):
        return None
    mimetype = part.get_content_type()
    too_small = mimetype.startswith("image/") and len(data) < MIN_IMAGE_BYTES
    if not data or too_small or len(data) > MAX_ATTACHMENT_BYTES:
        return None
    return Attachment(part.get_filename() or "attachment", mimetype, data, origin="email")


def _text_of(message: Any) -> str:
    try:
        body = message.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body is not None else ""
        if body is not None and body.get_content_subtype() == "html":
            text = _html_to_text(text)
    except (KeyError, LookupError, ValueError):
        text = ""
    return "\n".join(line.rstrip() for line in text.splitlines())


def read_message(raw: bytes) -> dict[str, Any]:
    """A whole email: headers, its text, its attachments' names, and in ``files`` the
    attachments worth keeping (documents, not logos)."""
    message = email.message_from_bytes(raw, policy=default_policy)
    text = _text_of(message)
    parts = list(message.iter_attachments())  # type: ignore[attr-defined]
    attachments = [
        {"name": part.get_filename() or "(no name)", "type": part.get_content_type()}
        for part in parts
    ]
    return {
        "files": [f for f in map(_attachment_file, parts) if f is not None],
        "from": str(message["from"] or ""),
        "to": str(message["to"] or ""),
        "cc": str(message["cc"] or ""),
        "subject": str(message["subject"] or "(no subject)"),
        "date": str(message["date"] or ""),
        "text": text[:MAX_BODY_CHARS],
        "attachments": attachments,
    }


def draft_files(message: Any) -> list[Attachment]:
    """Every file a draft carries, small images included: changing it keeps them all."""
    files = []
    for part in message.iter_attachments():
        try:
            data = part.get_payload(decode=True) or b""
        except (LookupError, ValueError):
            continue
        files.append(Attachment(part.get_filename() or "attachment", part.get_content_type(), data))
    return files


def draft_id(value: Any) -> str:
    uid = str(value or "").strip()
    if not uid.isdigit():
        raise ToolError("id must be the draft id given by list_email_drafts.")
    return uid


def _shown(value: Any, fallback: str, length: int = 80) -> str:
    # Written by others or steered by what they wrote: it can't pass for the end of the
    # line the user approves. Tool.summary already turns control and bidi characters into
    # a visible mark.
    return re.sub(r"[«»‹›\"“”]", "", str(value or fallback))[:length]


def search_uids(imap: imaplib.IMAP4, criteria: str, literal: str = "") -> set[bytes]:
    """The UIDs found; a refused search is an error, never read as a list of UIDs.
    ``literal``: a word with accents, sent in UTF-8 after the criteria, which end with its
    key (SUBJECT, TEXT...)."""
    if literal:
        imap.literal = literal.encode()  # type: ignore[attr-defined]
        status, data = imap.uid("SEARCH", "CHARSET", "UTF-8", criteria)
    else:
        status, data = imap.uid("SEARCH", None, criteria)
    if status != "OK":
        raise imaplib.IMAP4.error(f"search refused: {criteria[:60]}")
    # Some servers (iCloud) send no untagged SEARCH line at all when nothing matches.
    tokens = data[0].split() if data and data[0] else []
    return {t for t in tokens if t.isdigit()}


_SEARCH_KEYS = {"from": "FROM", "subject": "SUBJECT", "text": "TEXT"}


def search_criteria(terms: dict[str, str]) -> tuple[str, str]:
    """IMAP SEARCH keys for the terms (all must match), and the one term with accents,
    which goes last as a UTF-8 literal: the server only takes one."""
    parts, accented = [], []
    for name, value in terms.items():
        value = " ".join(str(value).split())
        if not value:
            continue
        if len(value) > 100 or not value.isprintable() or any(c in value for c in '"\\'):
            raise ToolError(f"{name} is plain words or an address, 100 characters max.")
        if name == "to":
            if not value.isascii():
                raise ToolError("to is an address or a part of one, without accents.")
            parts.append(f'OR TO "{value}" CC "{value}"')
        elif value.isascii():
            parts.append(f'{_SEARCH_KEYS[name]} "{value}"')
        else:
            accented.append((_SEARCH_KEYS[name], value))
    if len(accented) > 1:
        raise ToolError("Only one search term with accents at a time: leave out the others.")
    if accented:
        parts.append(accented[0][0])
    if not parts:
        raise ToolError("Give at least one of from, to, subject or text.")
    return " ".join(parts), accented[0][1] if accented else ""


def encode_folder(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501), e.g. Zoé -> Zo&AOk-."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            encoded = base64.b64encode("".join(run).encode("utf-16-be")).decode()
            out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            run.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


def decode_folder(name: str) -> str:
    def decode(match: re.Match[str]) -> str:
        encoded = match.group(1).replace(",", "/")
        if not encoded:
            return "&"
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-16-be")

    return re.sub(r"&([A-Za-z0-9+,]*)-", decode, name)


def _quoted(name: str) -> str:
    escaped = encode_folder(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(frozen=True)
class Folder:
    name: str
    flags: frozenset[str]
    # What splits a folder from its sub-folders on this server: "/" or ".", from LIST.
    delimiter: str = "/"

    @property
    def _leaf(self) -> str:
        name = self.name.casefold()
        return name.rsplit(self.delimiter, 1)[-1] if self.delimiter else name

    @property
    def _plain(self) -> str:
        """The name without the "INBOX." some servers put before every folder."""
        name = self.name.casefold()
        prefix = f"inbox{self.delimiter}"
        return name[len(prefix) :] if self.delimiter and name.startswith(prefix) else name

    @property
    def files_mail(self) -> bool:
        """One of the user's own folders, where filing an email can't hide or delete it."""
        return not self.flags & _SPECIAL_USE and self._plain not in _SPECIAL_NAMES

    @property
    def is_all(self) -> bool:
        return "\\all" in self.flags

    @property
    def is_trash(self) -> bool:
        return "\\trash" in self.flags or self._leaf in _TRASH_NAMES

    @property
    def is_sent(self) -> bool:
        return "\\sent" in self.flags or self._leaf in _SENT_NAMES

    @property
    def is_drafts(self) -> bool:
        return "\\drafts" in self.flags or self._leaf in _DRAFT_NAMES


def email_id(uid: str, folder: str, inbox: str) -> str:
    """The id the model gets: the UID alone in the inbox, "UID@folder" elsewhere."""
    return uid if folder == inbox else f"{uid}@{folder}"


def split_id(value: str, inbox: str) -> tuple[str, str]:
    uid, _, folder = str(value).strip().partition("@")
    if not uid.isdigit():
        raise ToolError("id must be the email id given by get_recent_emails.")
    return uid, folder or inbox


def new_folder_name(value: str, delimiter: str = "/") -> str:
    """The folder to create, as the server names it: the model writes Parent/Child, the
    server may split with another character."""
    parts = [" ".join(part.split()) for part in str(value).strip().strip("/").split("/")]
    name = "/".join(parts)
    # % and * are LIST wildcards; quotes and backslashes would break out of the name.
    if (
        not all(parts)
        or len(name) > 100
        or not name.isprintable()
        or any(c in name for c in '%*"\\')
        or (delimiter not in ("", "/") and delimiter in name)
    ):
        raise ToolError("A folder name is plain words, sub-folders split by /, 100 characters max.")
    if parts[-1].casefold() in _SPECIAL_NAMES or parts[0].startswith("["):
        raise ToolError(f"{name} is one of the server's own mailboxes, not a folder to create.")
    return (delimiter or "/").join(parts)


def parse_folder(line: Any) -> Folder | None:
    if not isinstance(line, bytes):
        return None
    match = _LIST_RE.match(line.decode("utf-8", "replace"))
    if not match:
        return None
    name = match["name"]
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    flags = frozenset(flag.casefold() for flag in match["flags"].split())
    delimiter = re.sub(r"\\(.)", r"\1", match["delimiter"] or "")
    return Folder(decode_folder(name), flags, delimiter)


_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


def fetched_with_flags(data: list[Any]) -> Iterator[tuple[bytes, bytes, bool]]:
    """Each fetched email's UID, bytes and whether it is read. A server may send FLAGS
    after the email's bytes, in the line that closes it, rather than before them."""
    items: list[list[Any]] = []
    for item in data:
        if isinstance(item, tuple):
            items.append([item[0], item[1]])
        elif isinstance(item, bytes) and items:
            items[-1][0] += b" " + item
    for head, raw in items:
        match = _UID_RE.search(head)
        flags = _FLAGS_RE.search(head)
        seen = bool(flags) and b"\\seen" in flags.group(1).lower()
        yield (match.group(1) if match else b""), raw, seen


def _capabilities(imap: imaplib.IMAP4) -> set[str]:
    # Asked again once logged in: servers announce MOVE or UIDPLUS only then.
    _, data = imap.capability()
    return {c.decode().upper() for c in (data[0] or b"").split()} if data else set()


def _select(imap: imaplib.IMAP4, folder: str, readonly: bool) -> None:
    status, _ = imap.select(_quoted(folder), readonly=readonly)
    if status != "OK":
        raise imaplib.IMAP4.error(f"cannot open {folder}")


def _message_id(raw: bytes) -> str:
    message = email.message_from_bytes(raw, policy=default_policy)
    value = str(message["message-id"] or "").strip()
    # Searched for in the destination to check the copy: it goes in a quoted string.
    if not value or not value.isprintable() or any(c in value for c in '"\\'):
        raise ToolError("This email has no usable Message-ID: its copy couldn't be checked.")
    return value


class ImapMailbox:
    def __init__(self, account: MailAccount) -> None:
        self.name = account.name
        self._account = account

    @contextmanager
    def _session(self, timeout: int = 15) -> Iterator[imaplib.IMAP4]:
        acc = self._account
        with _slots_lock:
            slots = _slots.setdefault(
                (acc.host, acc.username), threading.BoundedSemaphore(MAX_SESSIONS_PER_ACCOUNT)
            )
        with slots:
            for attempt in range(BUSY_RETRIES + 1):
                imap = imaplib.IMAP4_SSL(acc.host, acc.port, timeout=timeout)
                try:
                    imap.login(acc.username, acc.password.get_secret_value())
                except imaplib.IMAP4.error as exc:
                    imap.shutdown()
                    if attempt == BUSY_RETRIES or not _BUSY.search(str(exc)):
                        raise
                    time.sleep(BUSY_WAIT_SECONDS * (attempt + 1))
                    continue
                with imap:
                    yield imap
                return

    def _searched(
        self, imap: imaplib.IMAP4, received: bool = True, sent: bool = False
    ) -> list[Folder]:
        """Where received emails may be: the user may have deleted or filed them since. The
        inbox first, then the user's folders (Gmail: All Mail, which holds every label),
        then the trash. With sent, the sent emails' folder too."""
        folders = self._list(imap)
        selectable = [f for f in folders if "\\noselect" not in f.flags]
        found = []
        if received:
            inbox = Folder(self._account.mailbox, frozenset())
            # Only Gmail understands the filter All Mail is searched with.
            gmail = "X-GM-EXT-1" in _capabilities(imap)
            everything = [f for f in folders if f.is_all] if gmail else []
            own = everything or [f for f in folders if f.files_mail]
            found = [inbox, *own, *(f for f in selectable if f.is_trash)]
        if sent and (folder := self._special(folders, "\\sent", self._account.sent)):
            found.append(folder)
        return found

    def _special(self, folders: list[Folder], flag: str, configured: str | None) -> Folder | None:
        """The sent, drafts or trash folder: the one the account names, else the one the
        server marks, else the one with a usual name. The mark comes before the names: a
        folder of the user's own could be called "Sent"."""
        selectable = [f for f in folders if "\\noselect" not in f.flags]
        by_name = {"\\sent": "is_sent", "\\drafts": "is_drafts", "\\trash": "is_trash"}[flag]
        wanted = (configured or "").casefold()
        named = [f for f in selectable if wanted and f.name.casefold() == wanted]
        marked = [f for f in selectable if flag in f.flags]
        usual = [f for f in selectable if getattr(f, by_name)]
        return next(iter(named or marked or usual), None)

    def _drafts(self, imap: imaplib.IMAP4) -> str:
        """The folder drafts are saved in. Its name differs by provider and language
        ("Drafts", "[Gmail]/Drafts", "INBOX.Drafts", "Brouillons"...)."""
        found = self._special(self._list(imap), "\\drafts", self._account.drafts)
        return found.name if found else self._account.drafts or "Drafts"

    def _open(
        self, imap: imaplib.IMAP4, email: str, readonly: bool, sent: bool = False
    ) -> tuple[str, Folder]:
        """Select the email's folder, which must be one get_recent_emails searches, or the
        sent emails' when reading: an id can't reach the drafts or a made-up folder. In
        Gmail's All Mail, which holds those too, the email itself must pass the same
        filter."""
        uid, name = split_id(email, self._account.mailbox)
        folder = next((f for f in self._searched(imap, sent=sent) if f.name == name), None)
        if folder is None:
            raise ToolError("id must be the email id given by get_recent_emails.")
        _select(imap, folder.name, readonly=readonly)
        if folder.is_all and not search_uids(imap, f"UID {uid} {_ALL_MAIL_FILTER}"):
            raise ToolError("id must be the email id given by get_recent_emails.")
        return uid, folder

    def _fetch(
        self,
        since: datetime,
        unread_only: bool,
        count: int = MAX_MESSAGES_PER_ACCOUNT,
        terms: dict[str, str] | None = None,
        received: bool = True,
        sent: bool = False,
    ) -> list[MailSummary]:
        inbox = self._account.mailbox
        criteria = f'SINCE "{since.strftime("%d-%b-%Y")}"'
        keys, literal = search_criteria(terms) if terms else ("", "")
        summaries: list[MailSummary] = []
        with self._session(timeout=30) as imap:
            for folder in self._searched(imap, received, sent):
                try:
                    _select(imap, folder.name, readonly=True)
                except imaplib.IMAP4.error:
                    continue
                # All Mail also holds the inbox, sent emails and drafts: only the others.
                # The search keys go last: a word with accents follows them.
                # One search per folder: a TEXT search over a year of All Mail takes long,
                # so whether each email is read comes from its flags, fetched with it.
                wanted = [criteria, _ALL_MAIL_FILTER if folder.is_all else ""]
                if unread_only:
                    wanted.append("UNSEEN")
                uids = search_uids(imap, " ".join(filter(None, [*wanted, keys])), literal)
                newest = sorted(uids, key=int)[-count:]
                if not newest:
                    continue
                _, data = imap.uid(
                    "FETCH", b",".join(newest), f"(FLAGS BODY.PEEK[]<0.{_PARTIAL_BYTES}>)"
                )
                for uid, raw, seen in fetched_with_flags(data):
                    summary = parse_message(raw, self.name, unread=unread_only or not seen)
                    if summary is None or summary.received < since:
                        continue
                    summaries.append(
                        replace(
                            summary,
                            uid=email_id(uid.decode(), folder.name, inbox),
                            folder=folder.name,
                            deleted=folder.is_trash,
                            sent=folder.is_sent,
                        )
                    )
        summaries.sort(key=lambda s: s.received, reverse=True)
        return summaries[:count]

    async def recent(
        self, since: datetime, unread_only: bool, count: int = MAX_MESSAGES_PER_ACCOUNT
    ) -> list[MailSummary]:
        return await self._run(self._fetch, since, unread_only, count)

    async def search(
        self,
        terms: dict[str, str],
        since: datetime,
        count: int,
        received: bool = True,
        sent: bool = True,
    ) -> list[MailSummary]:
        """The newest emails since then matching every term, from the same folders and
        the sent emails'."""
        return await self._run(self._fetch, since, False, count, terms, received, sent)

    def _fetch_one(self, email: str, part: str = "BODY.PEEK[]", sent: bool = False) -> bytes | None:
        with self._session() as imap:
            uid, _ = self._open(imap, email, readonly=True, sent=sent)
            _, data = imap.uid("FETCH", uid, f"({part})")
        return next((item[1] for item in data if isinstance(item, tuple)), None)

    async def message(self, uid: str) -> dict[str, Any]:
        split_id(uid, self._account.mailbox)
        try:
            raw = await asyncio.to_thread(self._fetch_one, uid, "BODY.PEEK[]", True)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ToolError(f"Mailbox {self.name} unavailable: {exc}") from exc
        if raw is None:
            raise ToolError("No email with this id.")
        return read_message(raw)

    def _store_flag(self, email: str) -> bool:
        with self._session() as imap:
            uid, _ = self._open(imap, email, readonly=False)
            status, data = imap.uid("STORE", uid, "+FLAGS", r"(\Flagged)")
        return status == "OK" and bool(data and data[0])

    async def flag(self, uid: str) -> None:
        split_id(uid, self._account.mailbox)
        try:
            found = await asyncio.to_thread(self._store_flag, uid)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ToolError(f"Mailbox {self.name} unavailable: {exc}") from exc
        if not found:
            raise ToolError("No email with this id: it may have been deleted.")

    @property
    def address(self) -> str:
        return self._account.address or self._account.username

    @property
    def writable(self) -> bool:
        return self._account.write

    def _append_to_drafts(self, imap: imaplib.IMAP4, message: bytes) -> None:
        drafts = self._drafts(imap)
        status, data = imap.append(
            _quoted(drafts),
            r"(\Draft \Seen)",
            imaplib.Time2Internaldate(time.time()),
            message,
        )
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot save to {drafts}: {data}")

    def _append_draft(self, message: bytes) -> None:
        with self._session(timeout=30) as imap:
            self._append_to_drafts(imap, message)

    async def save_draft(self, message: bytes) -> None:
        try:
            await asyncio.to_thread(self._append_draft, message)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ToolError(f"Mailbox {self.name} unavailable: {exc}") from exc

    def _list_drafts(self) -> list[dict[str, Any]]:
        with self._session(timeout=30) as imap:
            _select(imap, self._drafts(imap), readonly=True)
            newest = sorted(search_uids(imap, "UNDELETED"), key=int)[-MAX_DRAFTS:]
            if not newest:
                return []
            _, data = imap.uid("FETCH", b",".join(newest), "(BODY.PEEK[])")
        drafts = []
        for item in data:
            if not isinstance(item, tuple):
                continue
            match = _UID_RE.search(item[0])
            message = read_message(item[1])
            message.pop("files")
            message["text"] = message["text"][:MAX_DRAFT_TEXT]
            drafts.append({"id": match.group(1).decode() if match else "", **message})
        return sorted(drafts, key=lambda d: int(d["id"] or 0), reverse=True)

    async def drafts(self) -> list[dict[str, Any]]:
        return await self._run(self._list_drafts)

    def _fetch_draft(self, uid: str, part: str = "BODY.PEEK[]") -> bytes:
        with self._session() as imap:
            _select(imap, self._drafts(imap), readonly=True)
            _, data = imap.uid("FETCH", uid, f"({part})")
        raw = next((item[1] for item in data if isinstance(item, tuple)), None)
        if raw is None:
            raise ToolError("No draft with this id: it may have been sent, changed or deleted.")
        return raw

    async def draft(self, uid: str) -> bytes:
        return await self._run(self._fetch_draft, draft_id(uid))

    async def draft_headline(self, uid: str) -> tuple[str, str]:
        """Its subject and recipients, for the user to see which draft changes."""
        raw = await self._run(
            self._fetch_draft, draft_id(uid), "BODY.PEEK[HEADER.FIELDS (TO CC SUBJECT)]"
        )
        message = email.message_from_bytes(raw, policy=default_policy)
        to = ", ".join(str(message[h]) for h in ("to", "cc") if message[h])
        return _shown(message["subject"], "(no subject)"), _shown(to, "?", 160)

    def _trash_draft(self, imap: imaplib.IMAP4, uid: str) -> str | None:
        """Move the selected draft to the trash, where the user can still get it back; None
        when the server has no trash or can't take only this draft out of the drafts."""
        trash = self._special(self._list(imap), "\\trash", self._account.trash)
        capabilities = _capabilities(imap)
        if trash is None or not {"MOVE", "UIDPLUS"} & capabilities:
            return None
        if "MOVE" in capabilities:
            status, data = imap.uid("MOVE", uid, _quoted(trash.name))
        else:
            status, data = imap.uid("COPY", uid, _quoted(trash.name))
            if status == "OK":
                imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
                imap.uid("EXPUNGE", uid)
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot move the draft to {trash.name}: {data}")
        return trash.name

    def _open_draft(self, imap: imaplib.IMAP4, uid: str) -> None:
        _select(imap, self._drafts(imap), readonly=False)
        if not search_uids(imap, f"UID {uid}"):
            raise ToolError("No draft with this id: it may have been sent, changed or deleted.")

    def _delete_draft(self, uid: str) -> dict[str, Any]:
        with self._session() as imap:
            self._open_draft(imap, uid)
            trash = self._trash_draft(imap, uid)
        if trash is None:
            raise ToolError(
                f"{self.name} can't put this draft in the trash: the user deletes it in their "
                "mail app."
            )
        return {"deleted": True, "in_trash": f"{self.name}/{trash}"}

    async def delete_draft(self, uid: str) -> dict[str, Any]:
        return await self._run(self._delete_draft, draft_id(uid))

    def _replace_draft(self, uid: str, message: bytes) -> dict[str, Any]:
        with self._session(timeout=30) as imap:
            self._open_draft(imap, uid)
            self._append_to_drafts(imap, message)
            trash = self._trash_draft(imap, uid)
        return {
            "saved_in": f"{self.name} drafts",
            "old_draft": f"in the trash ({trash})"
            if trash
            else "still in the drafts: this server can't remove it, the user deletes it",
        }

    async def replace_draft(self, uid: str, message: bytes) -> dict[str, Any]:
        """Save the changed draft, then put the old one in the trash."""
        return await self._run(self._replace_draft, draft_id(uid), message)

    def _list(self, imap: imaplib.IMAP4) -> list[Folder]:
        status, data = imap.list()
        if status != "OK":
            raise imaplib.IMAP4.error("cannot list the folders")
        folders = [f for f in map(parse_folder, data) if f is not None]
        # The folders the account names are special whatever the server says of them.
        acc = self._account
        named = {
            name.casefold(): flag
            for flag, name in (
                ("\\drafts", acc.drafts),
                ("\\sent", acc.sent),
                ("\\trash", acc.trash),
            )
            if name
        }
        return [
            replace(f, flags=f.flags | {named[f.name.casefold()]})
            if f.name.casefold() in named
            else f
            for f in folders
        ]

    def _destination(self, imap: imaplib.IMAP4, name: str) -> Folder:
        folders = self._list(imap)
        wanted = name.strip().strip("/").casefold()
        # As the server names it, or as the model may write it: Parent/Child, and without
        # the "INBOX." some servers put before every folder.
        folder = next((f for f in folders if f.name.casefold() == wanted), None) or next(
            (f for f in folders if wanted in {f._plain, f._plain.replace(f.delimiter or "/", "/")}),
            None,
        )
        if folder is None:
            own = [f.name for f in folders if f.files_mail]
            raise ToolError(f"No folder {name} in {self.name}. Its folders: {own}")
        if not folder.files_mail:
            raise ToolError(
                f"{folder.name} is not one of the user's own folders: never file there."
            )
        return folder

    def _list_folders(self) -> list[str]:
        with self._session() as imap:
            return sorted(f.name for f in self._list(imap) if f.files_mail)

    async def folders(self) -> list[str]:
        return await self._run(self._list_folders)

    def _create(self, value: str) -> dict[str, Any]:
        with self._session() as imap:
            folders = self._list(imap)
            delimiter = next((f.delimiter for f in folders if f.delimiter), "/")
            name = new_folder_name(value, delimiter)
            # Some servers keep every folder under the inbox ("INBOX.Invoices"): a new one
            # goes there too.
            prefix = f"{self._account.mailbox}{delimiter}"
            others = [f for f in folders if f.name.casefold() != self._account.mailbox.casefold()]
            if others and all(f.name.casefold().startswith(prefix.casefold()) for f in others):
                name = prefix + name
            existing = next((f for f in folders if f.name.casefold() == name.casefold()), None)
            if existing is not None:
                return {"folder": existing.name, "created": False}
            status, data = imap.create(_quoted(name))
            if status != "OK":
                raise imaplib.IMAP4.error(f"cannot create {name}: {data}")
            folder = self._destination(imap, name)
        return {"folder": folder.name, "created": True}

    async def create_folder(self, name: str) -> dict[str, Any]:
        """A new folder of the user's own; one already there is left as it is."""
        new_folder_name(name)  # an odd name is refused before any login
        return await self._run(self._create, name)

    async def headline(self, uid: str) -> tuple[str, str]:
        """The email's subject and who sent it, for the user to see which one moves."""
        split_id(uid, self._account.mailbox)
        raw = await self._run(self._fetch_one, uid, "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)]")
        if raw is None:
            raise ToolError("No email with this id: it may have been moved or deleted.")
        message = email.message_from_bytes(raw, policy=default_policy)
        return _shown(message["subject"], "(no subject)"), _shown(message["from"], "?")

    def _move_inside(self, email: str, name: str) -> dict[str, Any]:
        with self._session() as imap:
            capabilities = _capabilities(imap)
            folder = self._destination(imap, name)
            uid, source = self._open(imap, email, readonly=False)
            if not search_uids(imap, f"UID {uid}"):
                raise ToolError("No email with this id: it may have been moved or deleted.")
            if source.is_all:
                # In Gmail, a move out of All Mail can take the email away: only add the label.
                status, data = imap.uid("COPY", uid, _quoted(folder.name))
                if status != "OK":
                    raise imaplib.IMAP4.error(f"cannot copy to {folder.name}: {data}")
                return {"labelled": f"{self.name}/{folder.name}"}
            if "MOVE" in capabilities:
                status, data = imap.uid("MOVE", uid, _quoted(folder.name))
                if status != "OK":
                    raise imaplib.IMAP4.error(f"cannot move to {folder.name}: {data}")
                return {"moved_to": f"{self.name}/{folder.name}"}
            status, data = imap.uid("COPY", uid, _quoted(folder.name))
            if status != "OK":
                raise imaplib.IMAP4.error(f"cannot copy to {folder.name}: {data}")
            if "UIDPLUS" not in capabilities:
                # Without UID EXPUNGE, removing the original could also wipe other emails
                # marked deleted in the inbox: it stays.
                return {
                    "copied_to": f"{self.name}/{folder.name}",
                    "original": f"still in {source.name}: this server can't move it",
                }
            # What MOVE does, on a server without it: only this email leaves the inbox,
            # once its copy is in the folder.
            imap.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
            imap.uid("EXPUNGE", uid)
            return {"moved_to": f"{self.name}/{folder.name}"}

    def _take(self, email: str) -> tuple[bytes, str, str]:
        """The whole email, the flags worth keeping and when it was received."""
        with self._session(timeout=30) as imap:
            uid, _ = self._open(imap, email, readonly=True)
            _, data = imap.uid("FETCH", uid, "(FLAGS INTERNALDATE BODY.PEEK[])")
        item = next((item for item in data if isinstance(item, tuple)), None)
        if item is None:
            raise ToolError("No email with this id: it may have been moved or deleted.")
        flags = [
            f.decode() for f in imaplib.ParseFlags(item[0]) if f.decode().casefold() in _KEPT_FLAGS
        ]
        received = imaplib.Internaldate2tuple(item[0])
        date = imaplib.Time2Internaldate(received if received else time.time())
        return item[1], f"({' '.join(flags)})", date

    def _holds(self, imap: imaplib.IMAP4, folder: Folder, message_id: str) -> bool:
        _select(imap, folder.name, readonly=True)
        return bool(search_uids(imap, f'HEADER Message-ID "{message_id}"'))

    def _receive(self, name: str, raw: bytes, flags: str, date: str) -> str:
        message_id = _message_id(raw)
        with self._session(timeout=30) as imap:
            folder = self._destination(imap, name)
            # A second run (a retry, a "yes" sent twice) finds the copy and adds none.
            if not self._holds(imap, folder, message_id):
                status, data = imap.append(_quoted(folder.name), flags, date, raw)
                if status != "OK":
                    raise imaplib.IMAP4.error(f"cannot save to {folder.name}: {data}")
                if not self._holds(imap, folder, message_id):
                    raise imaplib.IMAP4.error(f"the copy can't be found in {folder.name}")
            return folder.name

    def _archive(self, email: str) -> str | None:
        """Gmail only: take the email out of the inbox. It stays in All Mail, so nothing is
        deleted; other servers have no such place and keep it in the inbox."""
        uid, source = split_id(email, self._account.mailbox)
        if source != self._account.mailbox:
            return None  # already out of the inbox
        with self._session() as imap:
            capabilities = _capabilities(imap)
            if not {"X-GM-EXT-1", "MOVE"} <= capabilities:
                return None
            archive = next((f for f in self._list(imap) if "\\all" in f.flags), None)
            if archive is None:
                return None
            _select(imap, source, readonly=False)
            status, _ = imap.uid("MOVE", uid, _quoted(archive.name))
        return archive.name if status == "OK" else None

    async def move(self, uid: str, folder: str, target: ImapMailbox) -> dict[str, Any]:
        """File an email into one of the user's folders, in this account or
        another one. Between accounts it is copied, checked there, then archived here
        when the server can (Gmail); it is never deleted."""
        _, source = split_id(uid, self._account.mailbox)
        if target is self:
            return await self._run(self._move_inside, uid, folder)
        raw, flags, date = await self._run(self._take, uid)
        filed = await target._run(target._receive, folder, raw, flags, date)
        try:
            archived = await self._run(self._archive, uid)
        except ToolError:
            archived = None
        return {
            "copied_to": f"{target.name}/{filed}",
            "original": f"archived in {self.name} ({archived})"
            if archived
            else f"still in {self.name} {source}",
        }

    async def _run(self, func: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ToolError(f"Mailbox {self.name} unavailable: {exc}") from exc


def build_draft_message(
    sender: str,
    to: list[str],
    cc: list[str],
    subject: str,
    body: str,
    attachments: list[Attachment],
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)
    for file in attachments:
        maintype, _, subtype = file.mimetype.partition("/")
        message.add_attachment(
            file.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=file.filename,
        )
    return message


def edited_draft(
    raw: bytes, sender: str, changes: dict[str, Any], added: list[Attachment]
) -> EmailMessage:
    """The draft with what changes: recipients, subject or text when given, its files
    kept but the removed ones, and the added ones."""
    old = email.message_from_bytes(raw, policy=default_policy)
    files = draft_files(old)
    removed = {safe_filename(str(n)) for n in changes.get("remove_attachments") or []}
    if unknown := removed - {f.filename for f in files}:
        raise ToolError(f"The draft has no files {sorted(unknown)}: {[f.filename for f in files]}")

    def kept(key: str) -> list[str]:
        if key in changes:
            return changes[key]
        return [str(old[key])] if old[key] else []

    message = build_draft_message(
        str(old["from"] or sender),
        kept("to"),
        kept("cc"),
        str(changes["subject"] if "subject" in changes else old["subject"] or ""),
        str(changes["body"] if "body" in changes else _text_of(old)),
        [f for f in files if f.filename not in removed] + added,
    )
    # A reply stays in its thread.
    for name in ("In-Reply-To", "References"):
        if old[name]:
            message[name] = str(old[name])
    return message


def build_draft_tools(mailboxes: list[ImapMailbox], language: str = "en") -> list[Tool]:
    """Drafts, flags and filing, for the accounts the user opened to writes."""
    if not mailboxes:
        return []
    s = strings(language)
    by_name = {m.name.casefold(): m for m in mailboxes}

    def addresses(value: Any) -> list[str]:
        items = value if isinstance(value, list) else str(value or "").split(",")
        return [item.strip() for item in items if str(item).strip()]

    def attached(args: dict[str, Any], ctx: ToolContext) -> tuple[list[str], list[str]]:
        """The kept files to attach, and those the user should check before sending."""
        files = ctx.conversation.files
        wanted = [str(n) for n in args.get("attachments") or []]
        missing = [n for n in wanted if n not in files]
        if missing:
            raise ToolError(f"Unknown files {missing}. Files the user sent: {list(files)}")
        # Text read in this request (an email, a web page) could be asking to send the
        # user's own files out. A draft is never sent, so it is allowed, but flagged under
        # the reply for the user to check the recipients before pressing Send.
        flagged = [n for n in wanted if files[n].origin != "user"] if ctx.tainted else []
        return wanted, flagged

    async def save_email_draft(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        mailbox = mailboxes[0]
        if name := args.get("account"):
            if name.casefold() not in by_name:
                raise ToolError(f"Unknown account {name}. Accounts: {[m.name for m in mailboxes]}")
            mailbox = by_name[name.casefold()]
        to = addresses(args.get("to"))
        if not to:
            raise ToolError("to is required.")
        files = ctx.conversation.files
        wanted, flagged = attached(args, ctx)
        cc = addresses(args.get("cc"))
        subject = str(args.get("subject") or "").strip()
        message = build_draft_message(
            mailbox.address,
            to,
            cc,
            subject,
            str(args.get("body") or "").strip(),
            [files[n] for n in wanted],
        )
        await mailbox.save_draft(message.as_bytes())
        # Reported to the user by the code, not the model: a draft an email or a file
        # slipped in can't go unnoticed.
        ctx.actions.append(
            {
                "type": "draft",
                "to": to + cc,
                "subject": subject,
                "attachments": wanted,
                "check": flagged,
            }
        )
        return {"saved_in": f"{mailbox.name} drafts", "to": to, "attachments": wanted}

    def account(value: Any) -> ImapMailbox:
        name = str(value or "")
        if name.casefold() not in by_name:
            raise ToolError(f"Unknown account {name}. Accounts: {[m.name for m in mailboxes]}")
        return by_name[name.casefold()]

    async def flag_email(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        await account(args.get("account")).flag(str(args.get("id") or ""))
        return {"flagged": True}

    async def list_email_folders(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        mailbox = account(args.get("account"))
        return {"account": mailbox.name, "folders": await mailbox.folders()}

    async def create_email_folder(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        mailbox = account(args.get("account"))
        return {"account": mailbox.name, **await mailbox.create_folder(str(args.get("name") or ""))}

    async def describe_create(args: dict[str, Any]) -> str:
        mailbox = account(args.get("account"))
        name = new_folder_name(str(args.get("name") or ""))
        return s.mail_create_folder_summary.format(name=name, account=mailbox.name)

    async def describe_flag(args: dict[str, Any]) -> str:
        mailbox = account(args.get("account"))
        subject, sender = await mailbox.headline(str(args.get("id") or ""))
        headline = s.mail_headline.format(subject=subject, sender=sender)
        return s.mail_flag_summary.format(headline=headline, account=mailbox.name)

    def move_target(args: dict[str, Any]) -> ImapMailbox:
        return account(args.get("to_account") or args.get("account"))

    async def move_email(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        source = account(args.get("account"))
        return await source.move(
            str(args.get("id") or ""), str(args.get("folder") or ""), move_target(args)
        )

    async def describe_move(args: dict[str, Any]) -> str:
        source, target = account(args.get("account")), move_target(args)
        subject, sender = await source.headline(str(args.get("id") or ""))
        return s.mail_move_summary.format(
            folder=args.get("folder"),
            target=target.name,
            source=source.name,
            headline=s.mail_headline.format(subject=subject, sender=sender),
        )

    async def list_email_drafts(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        mailbox = account(args.get("account"))
        return {"account": mailbox.name, "drafts": await mailbox.drafts()}

    def changes(args: dict[str, Any]) -> dict[str, Any]:
        found: dict[str, Any] = {k: addresses(args[k]) for k in ("to", "cc") if k in args}
        if "to" in found and not found["to"]:
            raise ToolError("to can't be emptied: a draft needs a recipient.")
        found.update({k: str(args[k]).strip() for k in ("subject", "body") if k in args})
        if "remove_attachments" in args:
            found["remove_attachments"] = [str(n) for n in args["remove_attachments"] or []]
        return found

    async def update_email_draft(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        mailbox = account(args.get("account"))
        wanted, flagged = attached(args, ctx)
        raw = await mailbox.draft(str(args.get("id") or ""))
        files = ctx.conversation.files
        message = edited_draft(raw, mailbox.address, changes(args), [files[n] for n in wanted])
        result = await mailbox.replace_draft(str(args.get("id") or ""), message.as_bytes())
        ctx.actions.append(
            {
                "type": "draft",
                "to": [str(message["to"] or ""), *([str(message["cc"])] if message["cc"] else [])],
                "subject": str(message["subject"] or ""),
                "attachments": [p.get_filename() for p in message.iter_attachments()],
                "check": flagged,
            }
        )
        return result

    async def draft_headline(mailbox: ImapMailbox, args: dict[str, Any]) -> str:
        subject, to = await mailbox.draft_headline(str(args.get("id") or ""))
        return s.draft_headline.format(subject=subject, to=to)

    async def describe_update(args: dict[str, Any]) -> str:
        mailbox = account(args.get("account"))
        headline = await draft_headline(mailbox, args)
        found = changes(args)
        # The recipients and files in full: that is what could take the user's data out.
        # The draft itself is never sent: the user reads the new text before pressing Send.
        done = [
            s.draft_change_recipients.format(field=k, value=", ".join(found[k]) or s.draft_nobody)
            for k in ("to", "cc")
            if k in found
        ]
        if "subject" in found:
            done.append(s.draft_change_subject.format(subject=found["subject"]))
        if "body" in found:
            text = " ".join(found["body"].split())
            shown = f"{text[:150]}{'…' if len(text) > 150 else ''}"
            done.append(s.draft_change_body.format(text=shown))
        if added := [str(n) for n in args.get("attachments") or []]:
            done.append(s.draft_change_attach.format(files=", ".join(added)))
        if found.get("remove_attachments"):
            done.append(s.draft_change_remove.format(files=", ".join(found["remove_attachments"])))
        return s.draft_update_summary.format(
            account=mailbox.name, headline=headline, what=" ; ".join(done) or s.draft_no_change
        )

    async def delete_email_draft(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        return await account(args.get("account")).delete_draft(str(args.get("id") or ""))

    async def describe_delete(args: dict[str, Any]) -> str:
        mailbox = account(args.get("account"))
        headline = await draft_headline(mailbox, args)
        return s.draft_delete_summary.format(account=mailbox.name, headline=headline)

    names = ", ".join(m.name for m in mailboxes)
    addresses_schema = {"type": "array", "items": {"type": "string"}}
    draft_schema = {
        "account": {"type": "string"},
        "id": {"type": "string", "description": "The draft's id from list_email_drafts."},
    }
    return [
        Tool(
            name="flag_email",
            description=(
                "Flag an email (a flag or a star in the user's mail app) to show the user "
                "you acted on it. Nothing else changes: never moved or deleted. "
                "By account and id from get_recent_emails. Only in a request that read "
                "nothing else."
            ),
            input_schema={
                "type": "object",
                "properties": {"account": {"type": "string"}, "id": {"type": "string"}},
                "required": ["account", "id"],
            },
            handler=flag_email,
            needs_clean_turn=True,
            proposable=True,
            describe=describe_flag,
        ),
        Tool(
            name="save_email_draft",
            description=(
                "Save an email as a draft in one of the user's mailboxes, with kept files "
                "(sent by the user, kept from an email, read from a drive or made here) as "
                "attachments. Nothing is sent: the user reviews and sends it from their mail "
                f"app. Accounts: {names} (default the first)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Plain text, signed as the user."},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of files kept in this conversation.",
                    },
                    "account": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
            handler=save_email_draft,
        ),
        Tool(
            name="list_email_folders",
            description=(
                "The user's own mail folders in one account (labels in Gmail), where "
                f"move_email can file an email. Accounts: {names}."
            ),
            input_schema={
                "type": "object",
                "properties": {"account": {"type": "string"}},
                "required": ["account"],
            },
            handler=list_email_folders,
        ),
        Tool(
            name="create_email_folder",
            description=(
                "Create a mail folder of the user's own in one account (a label in Gmail; "
                "Parent/Child for a sub-folder), so move_email can file there. "
                "It always waits for the user's go; one already there is left as it is."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "name": {"type": "string", "description": "e.g. Invoices/2026"},
                },
                "required": ["account", "name"],
            },
            handler=create_email_folder,
            always_propose=True,
            describe=describe_create,
        ),
        Tool(
            name="move_email",
            description=(
                "File an email (account and id from get_recent_emails) into one "
                "of the user's own folders, by its exact name from list_email_folders. "
                "to_account files it in another account: it is copied there, then the "
                "original is archived when its server has an archive that keeps everything "
                "(Gmail's All Mail), and left where it was elsewhere. Only when the user "
                "clearly asked to move or file "
                "that email. Never into the trash, junk, sent or drafts, never a folder that "
                "doesn't exist, and nothing is ever deleted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "id": {"type": "string"},
                    "folder": {"type": "string", "description": "e.g. Invoices/2026"},
                    "to_account": {
                        "type": "string",
                        "description": "Account of the folder; default: the email's own",
                    },
                },
                "required": ["account", "id", "folder"],
            },
            handler=move_email,
            needs_clean_turn=True,
            needs_user_request=True,
            proposable=True,
            describe=describe_move,
        ),
        Tool(
            name="list_email_drafts",
            description=(
                "The drafts waiting in one account's drafts folder, newest first: id, "
                "recipients, subject, text and attached files, to change or delete one. "
                f"Accounts: {names}."
            ),
            input_schema={
                "type": "object",
                "properties": {"account": {"type": "string"}},
                "required": ["account"],
            },
            handler=list_email_drafts,
            # Drafts can quote the emails they answer.
            taints=True,
        ),
        Tool(
            name="update_email_draft",
            description=(
                "Change a draft (account and id from list_email_drafts) instead of saving "
                "another one: give only what changes. to, cc, subject or body replace the "
                "draft's own (body is the whole new text); attachments adds files kept in "
                "this conversation by name; remove_attachments takes files off by their "
                "name in the draft. Its other files stay. The changed draft is saved and the "
                "old one goes to the trash. It always waits for the user's go."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **draft_schema,
                    "to": addresses_schema,
                    "cc": addresses_schema,
                    "subject": {"type": "string"},
                    "body": {"type": "string", "description": "Plain text, signed as the user."},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of files kept in this conversation.",
                    },
                    "remove_attachments": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["account", "id"],
            },
            handler=update_email_draft,
            always_propose=True,
            describe=describe_update,
            multiline_args=frozenset({"body"}),
        ),
        Tool(
            name="delete_email_draft",
            description=(
                "Delete a draft (account and id from list_email_drafts), e.g. an older or "
                "incomplete one the user no longer wants: it goes to the account's trash. "
                "It always waits for the user's go."
            ),
            input_schema={
                "type": "object",
                "properties": draft_schema,
                "required": ["account", "id"],
            },
            handler=delete_email_draft,
            always_propose=True,
            describe=describe_delete,
        ),
    ]


def build_email_tools(sources: list[MailSource], timezone: str) -> list[Tool]:
    if not sources:
        return []
    tz = ZoneInfo(timezone)
    by_name = {s.name.casefold(): s for s in sources}

    async def read_email(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("account") or "")
        if name.casefold() not in by_name:
            raise ToolError(f"Unknown account {name}. Accounts: {[s.name for s in sources]}")
        message = await by_name[name.casefold()].message(str(args.get("id") or ""))
        kept = [ctx.conversation.keep_file(f).filename for f in message.pop("files", [])]
        return {**message, "kept_files": kept}

    def selected(args: dict[str, Any]) -> list[MailSource]:
        if name := args.get("account"):
            if name.casefold() not in by_name:
                raise ToolError(f"Unknown account {name}. Accounts: {[s.name for s in sources]}")
            return [by_name[name.casefold()]]
        return sources

    def number(args: dict[str, Any], key: str, default: int, top: int, low: int = 1) -> int:
        return min(max(int(args.get(key, default)), low), top)

    async def gathered(
        calls: list[Any], offset: int, limit: int
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        """One page of the accounts' emails merged newest first, whether more are older,
        and the accounts that couldn't be read."""
        results = await asyncio.gather(*calls, return_exceptions=True)
        messages: list[MailSummary] = []
        errors = []
        for result in results:
            if isinstance(result, ToolError):
                errors.append(str(result))
            elif isinstance(result, BaseException):
                raise result
            else:
                messages.extend(result)
        messages.sort(key=lambda m: m.received, reverse=True)
        page = [
            {
                "account": m.account,
                "from": m.sender,
                "subject": m.subject,
                "received": m.received.astimezone(tz).isoformat(timespec="minutes"),
                "unread": m.unread,
                "snippet": m.snippet,
                "id": m.uid,
                **({"folder": m.folder} if m.folder and "@" in m.uid else {}),
                **({"deleted": True} if m.deleted else {}),
                **({"sent": True, "to": m.recipients} if m.sent else {}),
            }
            for m in messages[offset : offset + limit]
        ]
        return page, len(messages) > offset + limit, errors

    async def get_recent_emails(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        hours = number(args, "hours", 24, MAX_HOURS)
        limit = number(args, "limit", MAX_MESSAGES_PER_ACCOUNT, MAX_LIMIT)
        offset = number(args, "offset", 0, MAX_LISTED - limit, low=0)
        unread_only = bool(args.get("unread_only", True))
        since = datetime.now(UTC) - timedelta(hours=hours)
        # One more than the page, so the page says whether older ones remain.
        count = offset + limit + 1
        emails, more, errors = await gathered(
            [s.recent(since, unread_only, count) for s in selected(args)], offset, limit
        )
        return {
            "hours": hours,
            "unread_only": unread_only,
            "count": len(emails),
            "emails": emails,
            **({"more": f"older ones: offset {offset + limit}"} if more else {}),
            "errors": errors,
        }

    async def search_emails(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        terms = {k: str(args[k]) for k in ("from", "to", "subject", "text") if args.get(k)}
        search_criteria(terms)  # a term the server can't take is refused before any login
        days = number(args, "days", 365, MAX_SEARCH_DAYS)
        limit = number(args, "limit", MAX_MESSAGES_PER_ACCOUNT, MAX_LIMIT)
        since = datetime.now(UTC) - timedelta(days=days)
        sent = args.get("sent")
        emails, more, errors = await gathered(
            [
                s.search(terms, since, limit + 1, received=sent is not True, sent=sent is not False)
                for s in selected(args)
            ],
            0,
            limit,
        )
        return {
            "days": days,
            "count": len(emails),
            "emails": emails,
            **({"more": "older ones: narrow the search or lower days"} if more else {}),
            "errors": errors,
        }

    accounts = ", ".join(s.name for s in sources)
    return [
        Tool(
            name="get_recent_emails",
            description=(
                "Recent emails (sender, subject, start of the text) from the user's mailboxes, "
                "read-only, from every folder: the inbox, the user's folders and the trash "
                "(the user may have deleted or filed some since; deleted ones say so). Use "
                "it to spot what is important or needs an answer. Only unread ones unless "
                "unread_only is false: the emails the user already deleted or filed are "
                "usually read, so a look that must include them sets it false from the first "
                "call. Newest first, limit at a time (default "
                f"{MAX_MESSAGES_PER_ACCOUNT}, up to {MAX_LIMIT}: a long window or a sweep "
                "takes it whole with a higher limit); when more says older ones remain, call "
                f"again with that offset. Accounts: {accounts} (default all)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "minimum": 1, "maximum": MAX_HOURS},
                    "unread_only": {
                        "type": "boolean",
                        "description": "default true; false: read ones too",
                    },
                    "account": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    "offset": {"type": "integer", "minimum": 0},
                },
            },
            handler=get_recent_emails,
            taints=True,
        ),
        Tool(
            name="search_emails",
            description=(
                "Find emails, read or not, however old (days back, default 365), in the "
                "same folders as get_recent_emails and in the emails the user sent (those "
                "say sent, with their recipients): by sender (from: an address, a part of "
                "it or a name), recipient (to, also matches cc), words of the subject or of "
                "the text. Every term given must match. Newest first, with ids for "
                "read_email. Each search reads every folder of every account and can take a "
                "minute: set days from the user's words when they say roughly when, with a "
                'margin ("last week": 14), and search by sender first; after three '
                "searches that found nothing, say what you tried instead of trying more "
                f"spellings. Accounts: {accounts} (default all)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "e.g. sam@example.com"},
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "text": {"type": "string", "description": "e.g. warranty"},
                    "sent": {
                        "type": "boolean",
                        "description": "true: only what the user sent; false: only what "
                        "they received; default both",
                    },
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_DAYS,
                        "description": "default 365",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    "account": {"type": "string"},
                },
            },
            handler=search_emails,
            taints=True,
        ),
        Tool(
            name="read_email",
            description=(
                "Read one email whole (text, recipients, names of its attachments), by its "
                "account and id from get_recent_emails or search_emails (a sent one too), "
                "e.g. to see everything it asks for or what the user answered. "
                "Its documents are kept (kept_files): they can be attached to a draft by "
                "name, without reading the email again."
            ),
            input_schema={
                "type": "object",
                "properties": {"account": {"type": "string"}, "id": {"type": "string"}},
                "required": ["account", "id"],
            },
            handler=read_email,
            taints=True,
        ),
    ]
