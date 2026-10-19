"""Read-only access to the user's mailboxes over IMAP.

Mailboxes are opened read-only and bodies fetched with BODY.PEEK, so reading never marks
a message as seen.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from intelligent_home_agent.config import MailAccount
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

MAX_MESSAGES_PER_ACCOUNT = 25
SNIPPET_LENGTH = 300
# Enough for the headers and the start of the text part, without downloading attachments.
_PARTIAL_BYTES = 65536
_UID_RE = re.compile(rb"UID (\d+)")


@dataclass(frozen=True)
class MailSummary:
    account: str
    sender: str
    subject: str
    received: datetime
    unread: bool
    snippet: str


class MailSource(Protocol):
    name: str

    async def recent(self, since: datetime, unread_only: bool) -> list[MailSummary]: ...


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
    )


def search_uids(imap: imaplib.IMAP4, criteria: str) -> set[bytes]:
    data = imap.uid("SEARCH", None, criteria)[1]
    # Some servers send no untagged SEARCH line at all when nothing matches.
    return set(data[0].split()) if data and data[0] else set()


class ImapMailbox:
    def __init__(self, account: MailAccount) -> None:
        self.name = account.name
        self._account = account

    def _fetch(self, since: datetime, unread_only: bool) -> list[MailSummary]:
        acc = self._account
        criteria = f'SINCE "{since.strftime("%d-%b-%Y")}"'
        with imaplib.IMAP4_SSL(acc.host, acc.port, timeout=15) as imap:
            imap.login(acc.username, acc.password.get_secret_value())
            status, _ = imap.select(f'"{acc.mailbox}"', readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error(f"cannot open {acc.mailbox}")
            unseen = search_uids(imap, f"{criteria} UNSEEN")
            uids = unseen if unread_only else search_uids(imap, criteria)
            newest = sorted(uids, key=int)[-MAX_MESSAGES_PER_ACCOUNT:]
            if not newest:
                return []
            _, data = imap.uid("FETCH", b",".join(newest), f"(BODY.PEEK[]<0.{_PARTIAL_BYTES}>)")

        summaries = []
        for item in data:
            if not isinstance(item, tuple):
                continue
            match = _UID_RE.search(item[0])
            uid = match.group(1) if match else b""
            summary = parse_message(item[1], self.name, unread=uid in unseen)
            if summary is not None and summary.received >= since:
                summaries.append(summary)
        return summaries

    async def recent(self, since: datetime, unread_only: bool) -> list[MailSummary]:
        try:
            return await asyncio.to_thread(self._fetch, since, unread_only)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ToolError(f"Mailbox {self.name} unavailable: {exc}") from exc


def build_email_tools(sources: list[MailSource], timezone: str) -> list[Tool]:
    if not sources:
        return []
    tz = ZoneInfo(timezone)
    by_name = {s.name.casefold(): s for s in sources}

    async def get_recent_emails(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        hours = min(max(int(args.get("hours", 24)), 1), 168)
        unread_only = bool(args.get("unread_only", True))
        selected = sources
        if name := args.get("account"):
            if name.casefold() not in by_name:
                raise ToolError(f"Unknown account {name}. Accounts: {[s.name for s in sources]}")
            selected = [by_name[name.casefold()]]

        since = datetime.now(UTC) - timedelta(hours=hours)
        results = await asyncio.gather(
            *(s.recent(since, unread_only) for s in selected), return_exceptions=True
        )
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
        return {
            "hours": hours,
            "unread_only": unread_only,
            "count": len(messages),
            "emails": [
                {
                    "account": m.account,
                    "from": m.sender,
                    "subject": m.subject,
                    "received": m.received.astimezone(tz).isoformat(timespec="minutes"),
                    "unread": m.unread,
                    "snippet": m.snippet,
                }
                for m in messages
            ],
            "errors": errors,
        }

    return [
        Tool(
            name="get_recent_emails",
            description=(
                "Recent emails (sender, subject, start of the text) from the user's mailboxes, "
                "read-only. Use it to spot what is important or needs an answer. Accounts: "
                f"{', '.join(s.name for s in sources)}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "minimum": 1, "maximum": 168},
                    "unread_only": {"type": "boolean"},
                    "account": {"type": "string"},
                },
            },
            handler=get_recent_emails,
            taints=True,
        )
    ]
