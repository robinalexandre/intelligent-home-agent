import email
import imaplib
import re
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy

import pytest

from intelligent_home_agent.config import MailAccount
from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.main import with_waiting
from intelligent_home_agent.tools import (
    ImapMailbox,
    ToolContext,
    ToolError,
    ToolRegistry,
    build_draft_tools,
    build_email_tools,
)
from intelligent_home_agent.tools.mail import (
    MailSummary,
    decode_folder,
    encode_folder,
    fetched_with_flags,
    parse_folder,
    parse_message,
    read_message,
    search_criteria,
    search_uids,
)
from intelligent_home_agent.tools.proposals import build_proposal_tools


def raw_email(subject, body, html=False, date="Tue, 14 Oct 2026 07:12:00 +0200"):
    msg = EmailMessage()
    msg["From"] = "Jo Martin <jo@example.com>"
    msg["Subject"] = subject
    msg["Date"] = date
    if html:
        msg.set_content(
            "<html><head><style>p{}</style></head><body><p>Hello Sam,</p>"
            "<p>Can you approve the quote?</p></body></html>",
            subtype="html",
        )
    else:
        msg.set_content(body)
        msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="quote.pdf")
    return msg.as_bytes()


def test_parse_plain_message_with_attachment():
    summary = parse_message(raw_email("Quote", "Hello,\n\nhere is   the quote."), "Work", True)
    assert summary.sender == "Jo Martin <jo@example.com>"
    assert summary.subject == "Quote"
    assert summary.snippet == "Hello, here is the quote."
    assert summary.received == datetime(2026, 10, 14, 5, 12, tzinfo=UTC)
    assert summary.unread


def test_parse_html_only_message():
    summary = parse_message(raw_email("Quote", "", html=True), "Work", False)
    assert summary.snippet == "Hello Sam, Can you approve the quote?"


def test_message_without_date_is_skipped():
    assert parse_message(b"Subject: x\r\n\r\nbody", "Work", False) is None


def test_truncated_message_still_gives_headers():
    raw = raw_email("Invoice", "Amount: 1200")[:300]
    summary = parse_message(raw, "Work", True)
    assert summary.subject == "Invoice"


class FakeImap:
    def __init__(self, data):
        self._data = data

    def uid(self, *_args):
        return "OK", self._data


def test_search_uids_handles_empty_answers():
    assert search_uids(FakeImap([b"12 15"]), "ALL") == {b"12", b"15"}
    assert search_uids(FakeImap([b""]), "ALL") == set()
    assert search_uids(FakeImap([None]), "ALL") == set()


class FakeMailbox:
    def __init__(self, name, messages=None, error=None):
        self.name = name
        self._messages = messages or []
        self._error = error

    async def recent(self, since, unread_only, count=25):
        if self._error:
            raise ToolError(self._error)
        return [m for m in self._messages if m.received >= since][:count]

    async def search(self, terms, since, count, received=True, sent=True):
        self.searched = terms
        self.folders = (received, sent)
        return [m for m in self._messages if m.received >= since][:count]

    async def message(self, uid):
        return {"subject": f"message {uid} of {self.name}"}


def summary(account, subject, minutes_ago):
    received = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return MailSummary(account, "x@example.com", subject, received, True, "")


async def test_merges_accounts_newest_first_and_reports_failures(ctx):
    tools = build_email_tools(
        [
            FakeMailbox(
                "Personal", [summary("Personal", "old", 120), summary("Personal", "new", 5)]
            ),
            FakeMailbox("Work", [summary("Work", "mid", 60)]),
            FakeMailbox("Gmail", error="Mailbox Gmail unavailable: timeout"),
        ],
        "Europe/Paris",
    )
    tool, _, read = tools

    result = await tool.handler({"hours": 24}, ctx)

    assert [e["subject"] for e in result["emails"]] == ["new", "mid", "old"]
    assert result["errors"] == ["Mailbox Gmail unavailable: timeout"]
    assert await read.handler({"account": "work", "id": "42"}, ctx) == {
        "subject": "message 42 of Work",
        "kept_files": [],
    }
    assert read.taints


def test_read_message_gives_the_whole_text_and_attachment_names():
    raw = raw_email("Paperwork", "Hello,\n\n- Company extract\n- Tax certificate\n")
    message = read_message(raw)
    assert message["subject"] == "Paperwork"
    assert message["from"] == "Jo Martin <jo@example.com>"
    assert "- Tax certificate" in message["text"]
    assert message["attachments"] == [{"name": "quote.pdf", "type": "application/pdf"}]
    (kept,) = message["files"]
    assert (kept.filename, kept.data, kept.origin) == ("quote.pdf", b"%PDF", "email")


class OneMessage:
    name = "Work"

    async def message(self, _uid):
        msg = EmailMessage()
        msg["Subject"] = "Your invoice"
        msg.set_content("Here is your invoice.")
        msg.add_attachment(b"%PDF-acme", maintype="application", subtype="pdf", filename="F1.pdf")
        msg.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="logo.png")
        return read_message(msg.as_bytes())


async def test_an_emails_documents_are_kept_but_not_its_logos(ctx):
    *_, read = build_email_tools([OneMessage()], "Europe/Paris")  # type: ignore[list-item]
    result = await read.handler({"account": "Work", "id": "7"}, ctx)

    assert result["kept_files"] == ["F1.pdf"]
    assert "files" not in result
    assert ctx.conversation.files["F1.pdf"].origin == "email"


async def test_another_emails_file_of_the_same_name_never_replaces_the_first(ctx):
    first = Attachment("invoice.pdf", "application/pdf", b"%PDF-approved", "email")
    other = Attachment("invoice.pdf", "application/pdf", b"%PDF-swapped", "email")
    assert ctx.conversation.keep_file(first).filename == "invoice.pdf"
    assert ctx.conversation.keep_file(first).filename == "invoice.pdf"
    assert ctx.conversation.keep_file(other).filename == "invoice (email).pdf"

    assert ctx.conversation.files["invoice.pdf"].data == b"%PDF-approved"


class Flaggable:
    name = "Work"
    address = "work@example.com"

    def __init__(self) -> None:
        self.flagged: list[str] = []

    async def flag(self, uid: str) -> None:
        self.flagged.append(uid)


async def test_flagging_an_email_needs_a_clean_request(ctx):
    box = Flaggable()
    flag = {t.name: t for t in build_draft_tools([box])}["flag_email"]  # type: ignore[list-item]

    assert await flag.handler({"account": "work", "id": "12"}, ctx) == {"flagged": True}
    assert box.flagged == ["12"] and flag.needs_clean_turn


async def test_no_accounts_means_no_tool():
    assert build_email_tools([], "Europe/Paris") == []
    assert build_draft_tools([]) == []


class FakeDrafts:
    def __init__(self, name: str) -> None:
        self.name = name
        self.address = f"{name.lower()}@example.com"
        self.saved: list[bytes] = []

    async def save_draft(self, message: bytes) -> None:
        self.saved.append(message)


async def test_draft_is_saved_with_the_files_the_user_sent(ctx):
    personal, work = FakeDrafts("Personal"), FakeDrafts("Work")
    ctx.conversation.files["quote.pdf"] = Attachment("quote.pdf", "application/pdf", b"%PDF-1.7")
    tool = build_draft_tools([personal, work])[1]

    result = await tool.handler(
        {
            "to": ["jo@example.com"],
            "subject": "Quote",
            "body": "Hello Jo,\n\nHere is the quote.\n\nSam",
            "attachments": ["quote.pdf"],
            "account": "work",
        },
        ctx,
    )

    assert result == {
        "saved_in": "Work drafts",
        "to": ["jo@example.com"],
        "attachments": ["quote.pdf"],
    }
    assert personal.saved == []
    message = email.message_from_bytes(work.saved[0], policy=default_policy)
    assert message["From"] == "work@example.com"
    assert message["To"] == "jo@example.com"
    assert "Here is the quote." in message.get_body().get_content()
    (attachment,) = message.iter_attachments()
    assert attachment.get_filename() == "quote.pdf"
    assert attachment.get_content() == b"%PDF-1.7"
    assert not tool.needs_clean_turn


async def test_draft_refuses_files_that_were_never_sent(ctx):
    tool = build_draft_tools([FakeDrafts("Personal")])[1]
    with pytest.raises(ToolError, match="Unknown files"):
        await tool.handler(
            {"to": "a@b.c", "subject": "x", "body": "y", "attachments": ["/etc/passwd"]}, ctx
        )


def test_folder_names_use_modified_utf7():
    assert encode_folder("Zoé Client") == "Zo&AOk- Client"
    assert encode_folder("A&B") == "A&-B"
    assert decode_folder("Zo&AOk- Client") == "Zoé Client"
    assert decode_folder(encode_folder("Ünïcödé/日本 & co")) == "Ünïcödé/日本 & co"


def test_folder_lines_are_parsed():
    trash = parse_folder(b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"')
    assert trash.name == "Deleted Messages" and not trash.files_mail
    assert parse_folder(b'(\\HasNoChildren) "/" "Zo&AOk- Client"').files_mail
    assert parse_folder(b'(\\HasNoChildren) "/" Invoices').name == "Invoices"
    assert not parse_folder(b'(\\HasChildren) "/" "INBOX"').files_mail


def mail(message_id="<a1@example.com>", subject="Insurance certificate"):
    msg = EmailMessage()
    msg["From"] = "Kim Lee <kim@example.com>"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content("Here is the certificate.")
    return msg.as_bytes()


class FakeServer:
    """One IMAP account: folders, the emails in each, and every command it got."""

    def __init__(self, capabilities, folders=None, inbox=None):
        self.capabilities = capabilities
        self.folders = {"INBOX": "", "Deleted Messages": "\\Trash", **(folders or {})}
        self.boxes = {name: {} for name in self.folders}
        self.boxes["INBOX"] = dict(inbox or {})
        self.commands = []
        self.searches = []
        self.literal = None
        self.selected = "INBOX"
        self.unseen = set()
        self._uid = 100

    def _name(self, quoted):
        return decode_folder(quoted.strip('"'))

    def _add(self, name, raw):
        self._uid += 1
        self.boxes[name][str(self._uid)] = raw

    def capability(self):
        return "OK", [" ".join(self.capabilities).encode()]

    def list(self):
        return "OK", [
            f'({flags}) "/" "{encode_folder(name)}"'.encode()
            for name, flags in self.folders.items()
        ]

    def select(self, quoted, readonly=False):
        if self._name(quoted) not in self.boxes:
            return "NO", [b"no such folder"]
        self.selected = self._name(quoted)
        return "OK", [b"1"]

    def create(self, quoted):
        self.commands.append(("CREATE", self._name(quoted)))
        self.folders[self._name(quoted)] = "\\HasNoChildren"
        self.boxes[self._name(quoted)] = {}
        return "OK", [b""]

    def append(self, quoted, flags, _date, raw):
        self.commands.append(("APPEND", self._name(quoted), flags))
        self._add(self._name(quoted), raw)
        return "OK", [b""]

    def uid(self, command, *args):
        box = self.boxes[self.selected]
        self.commands.append((command, *args))
        if command == "SEARCH":
            criteria = args[1]
            if args[0] == "CHARSET":  # a word with accents follows as a UTF-8 literal
                criteria = f'{args[2]} "{self.literal.decode()}"'
                self.literal = None
            self.searches.append(criteria)
            # FROM, TO, SUBJECT...: each word sought must be in the email.
            words = {
                w.lower() for w in re.findall(r'(?:FROM|TO|CC|SUBJECT|TEXT) "([^"]*)"', criteria)
            }

            def matching(u):
                msg = email.message_from_bytes(box[u], policy=default_policy)
                body = msg.get_body(preferencelist=("plain",))
                seen = [str(msg[h] or "") for h in ("from", "to", "cc", "subject")]
                text = " ".join([*seen, body.get_content() if body else ""]).lower()
                return all(w in text for w in words)

            # Gmail's filter on All Mail: not in the inbox, not sent (a test marks those).
            def filtered(u):
                inbox = self.boxes.get("INBOX", {}).values()
                return "X-GM-RAW" not in criteria or (
                    box[u] not in inbox and b"X-Test-Sent" not in box[u]
                )

            if criteria.startswith("SINCE "):  # every email here is recent
                wanted = [u for u in box if filtered(u) and matching(u)]
                if "UNSEEN" in criteria:
                    wanted = [u for u in wanted if u in self.unseen]
                return "OK", [" ".join(wanted).encode()]
            if criteria.startswith("UID ") and "X-GM-RAW" in criteria:
                uid = criteria.split()[1]
                return "OK", [uid.encode() if uid in box and filtered(uid) else b""]
            if criteria.startswith("UID "):
                return "OK", [criteria[4:].encode() if criteria[4:] in box else b""]
            if criteria == "UNDELETED":
                return "OK", [" ".join(box).encode()]
            wanted = criteria.split('"')[1].encode()
            return "OK", [" ".join(u for u, raw in box.items() if wanted in raw).encode()]
        if command == "FETCH" and isinstance(args[0], bytes):  # several, by their headers

            def flags(u):
                return "" if u in self.unseen else "\\Seen"

            return "OK", [
                (f"1 (UID {u} FLAGS ({flags(u)}) BODY[] {{1}}".encode(), box[u])
                for u in args[0].decode().split(",")
                if u in box
            ]
        if command == "FETCH":
            if args[0] not in box:
                return "OK", [None]
            head = (
                f"1 (UID {args[0]} FLAGS (\\Seen \\Recent) "
                f'INTERNALDATE "14-Oct-2026 07:12:00 +0200" BODY[] {{{len(box[args[0]])}}}'
            )
            return "OK", [(head.encode(), box[args[0]]), b")"]
        if command in {"MOVE", "COPY"}:
            self._add(self._name(args[1]), box[args[0]])
            if command == "MOVE":
                del box[args[0]]
            return "OK", [b""]
        if command == "EXPUNGE":
            del box[args[0]]
        return "OK", [b"1"]


def mailbox(name, server):
    box = ImapMailbox(MailAccount(name=name, host="imap.test", username="u", password="p"))
    box._session = lambda timeout=15: nullcontext(server)  # type: ignore[method-assign]
    return box


def plain(capabilities=("IMAP4REV1", "MOVE"), inbox=None):
    return FakeServer(list(capabilities), {"Zoé Client": "\\HasNoChildren"}, inbox)


async def test_an_email_is_moved_into_a_folder_of_its_own_account():
    server = plain(inbox={"12": mail(), "13": mail("<other@example.com>")})
    box = mailbox("Home", server)

    assert await box.move("12", "zoé client", box) == {"moved_to": "Home/Zoé Client"}
    assert ("MOVE", "12", '"Zo&AOk- Client"') in server.commands
    assert list(server.boxes["INBOX"]) == ["13"]
    assert list(server.boxes["Zoé Client"].values()) == [mail()]


async def test_without_move_only_that_email_leaves_the_inbox_once_copied():
    server = plain(("IMAP4REV1", "UIDPLUS"), inbox={"12": mail(), "13": mail("<b@x>")})
    box = mailbox("Home", server)

    assert await box.move("12", "Zoé Client", box) == {"moved_to": "Home/Zoé Client"}
    commands = [c[0] for c in server.commands]
    assert commands[commands.index("COPY") :] == ["COPY", "STORE", "EXPUNGE"]
    assert ("EXPUNGE", "12") in server.commands
    assert list(server.boxes["INBOX"]) == ["13"]


async def test_without_move_or_uid_expunge_the_original_stays():
    server = plain(("IMAP4REV1",), inbox={"12": mail()})
    box = mailbox("Home", server)

    result = await box.move("12", "Zoé Client", box)

    assert result["copied_to"] == "Home/Zoé Client"
    assert "still in INBOX" in result["original"]
    assert "12" in server.boxes["INBOX"]
    assert not {"STORE", "EXPUNGE"} & {c[0] for c in server.commands}


@pytest.mark.parametrize("folder", ["Deleted Messages", "INBOX", "Nowhere"])
async def test_an_email_never_goes_to_the_trash_or_an_unknown_folder(folder):
    server = plain(inbox={"12": mail()})
    box = mailbox("Home", server)

    with pytest.raises(ToolError) as error:
        await box.move("12", folder, box)

    assert list(server.boxes["INBOX"]) == ["12"]
    assert not {"MOVE", "COPY"} & {c[0] for c in server.commands}
    if folder == "Nowhere":
        assert "['Zoé Client']" in str(error.value)


async def test_a_missing_email_moves_nothing():
    server = plain(inbox={})
    box = mailbox("Home", server)
    with pytest.raises(ToolError, match="No email with this id"):
        await box.move("12", "Zoé Client", box)


def gmail(inbox):
    return FakeServer(
        ["IMAP4REV1", "X-GM-EXT-1", "MOVE"],
        {"[Gmail]/All Mail": "\\All \\HasNoChildren", "[Gmail]/Trash": "\\Trash"},
        inbox,
    )


def dated(message_id, subject):
    msg = EmailMessage()
    msg["From"] = "shop@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content("Your parcel is on its way.")
    return msg.as_bytes()


async def test_recent_emails_come_from_every_folder_the_trash_included():
    server = plain(inbox={"12": dated("<a@x>", "Order")})
    server.boxes["Zoé Client"]["20"] = dated("<b@x>", "Certificate")
    server.boxes["Deleted Messages"]["30"] = dated("<c@x>", "Parcel shipped")
    box = mailbox("Home", server)

    found = await box.recent(datetime.now(UTC) - timedelta(hours=3), unread_only=False)
    by_subject = {m.subject: m for m in found}

    assert set(by_subject) == {"Order", "Certificate", "Parcel shipped"}
    assert by_subject["Order"].uid == "12" and not by_subject["Order"].deleted
    assert by_subject["Certificate"].uid == "20@Zoé Client"
    assert by_subject["Parcel shipped"].uid == "30@Deleted Messages"
    assert by_subject["Parcel shipped"].deleted
    assert (await box.message("30@Deleted Messages"))["subject"] == "Parcel shipped"


async def test_an_email_found_in_the_trash_can_be_filed_from_there():
    server = plain()
    server.boxes["Deleted Messages"]["30"] = mail()
    box = mailbox("Home", server)

    assert await box.move("30@Deleted Messages", "Zoé Client", box) == {
        "moved_to": "Home/Zoé Client"
    }
    assert server.boxes["Deleted Messages"] == {}


@pytest.mark.parametrize("email", ["5@Made up", "5@Drafts", "x@Zoé Client"])
async def test_an_id_only_reaches_the_folders_that_are_searched(email):
    server = plain()
    server.folders.update({"Sent Messages": "\\Sent", "Drafts": "\\Drafts"})
    server.boxes.update({"Sent Messages": {"5": mail()}, "Drafts": {"5": mail()}})
    box = mailbox("Home", server)

    with pytest.raises(ToolError, match="email id given by get_recent_emails"):
        await box.message(email)
    with pytest.raises(ToolError, match="email id given by get_recent_emails"):
        await box.flag(email)
    assert not any(c[0] in {"FETCH", "STORE"} for c in server.commands)


def to_max(subject, body, message_id):
    msg = EmailMessage()
    msg["From"] = "sam@example.com"
    msg["To"] = "Max <max.carter@example.com>"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(body)
    return msg.as_bytes()


async def test_sent_emails_are_searched_and_read_but_never_filed_or_flagged(ctx):
    server = plain(inbox={"12": from_kim("Maple Street", "The warranty of the boiler.", "<a@x>")})
    server.folders["Sent Messages"] = "\\Sent \\HasNoChildren"
    server.boxes["Sent Messages"] = {"40": to_max("Boiler", "The warranty covers 300.", "<b@x>")}
    box = mailbox("Home", server)
    recent, search, read = build_email_tools([box], "Europe/Paris")

    both = await search.handler({"text": "warranty"}, ctx)
    only_sent = await search.handler({"text": "warranty", "sent": True}, ctx)
    received = await search.handler({"text": "warranty", "sent": False}, ctx)
    listed = await recent.handler({"hours": 3, "unread_only": False}, ctx)

    assert sorted(e["id"] for e in both["emails"]) == ["12", "40@Sent Messages"]
    [answer] = only_sent["emails"]
    assert answer["sent"] and answer["to"] == "Max <max.carter@example.com>"
    assert [e["id"] for e in received["emails"]] == ["12"]
    assert [e["id"] for e in listed["emails"]] == ["12"]
    message = await read.handler({"account": "home", "id": "40@Sent Messages"}, ctx)
    assert "300" in message["text"]
    with pytest.raises(ToolError, match="email id given by get_recent_emails"):
        await box.flag("40@Sent Messages")
    with pytest.raises(ToolError, match="email id given by get_recent_emails"):
        await box.move("40@Sent Messages", "Zoé Client", box)
    assert list(server.boxes["Sent Messages"]) == ["40"]


async def test_gmail_sent_mail_is_found_by_its_mark_not_a_folder_named_sent(ctx):
    server = gmail({})
    server.folders.update({"Sent": "\\HasNoChildren", "[Gmail]/Messages envoyés": "\\Sent"})
    server.boxes["Sent"] = {"5": to_max("Boiler", "A folder of mine.", "<a@x>")}
    server.boxes["[Gmail]/Messages envoyés"] = {"9": to_max("Boiler", "Sent.", "<b@x>")}
    box = mailbox("Gmail", server)

    found = await box.search({"to": "max"}, datetime.now(UTC) - timedelta(days=1), 10, False)

    assert [m.uid for m in found] == ["9@[Gmail]/Messages envoyés"]


async def test_gmail_shows_an_inbox_email_once_and_archived_ones_from_all_mail():
    invoice = dated("<a@x>", "Invoice")
    sent = b"X-Test-Sent: 1\r\n" + dated("<c@x>", "My answer")
    server = gmail({"7": invoice})
    server.folders["Tracking"] = ""
    server.boxes["Tracking"] = {}
    server.boxes["[Gmail]/All Mail"] = {"70": invoice, "71": dated("<b@x>", "Tracking"), "72": sent}
    box = mailbox("Gmail", server)
    found = await box.recent(datetime.now(UTC) - timedelta(hours=3), False)

    assert sorted(m.uid for m in found) == ["7", "71@[Gmail]/All Mail"]
    with pytest.raises(ToolError, match="email id given by get_recent_emails"):
        await box.message("72@[Gmail]/All Mail")  # a sent email
    # Filing from All Mail only adds the label: nothing leaves All Mail.
    assert await box.move("71@[Gmail]/All Mail", "Tracking", box) == {"labelled": "Gmail/Tracking"}
    assert "71" in server.boxes["[Gmail]/All Mail"]
    assert not any(c[0] in {"MOVE", "EXPUNGE"} for c in server.commands)


async def test_a_gmail_email_is_filed_in_another_account_and_archived_in_gmail():
    source, target = gmail({"7": mail()}), plain()

    result = await mailbox("Gmail", source).move("7", "Zoé Client", mailbox("Home", target))

    assert result == {
        "copied_to": "Home/Zoé Client",
        "original": "archived in Gmail ([Gmail]/All Mail)",
    }
    assert ("APPEND", "Zoé Client", "(\\Seen)") in target.commands
    assert list(target.boxes["Zoé Client"].values()) == [mail()]
    assert source.boxes["INBOX"] == {} and source.boxes["[Gmail]/Trash"] == {}
    assert list(source.boxes["[Gmail]/All Mail"].values()) == [mail()]


async def test_an_email_already_filed_there_is_not_copied_twice():
    source, target = gmail({"7": mail()}), plain()
    target.boxes["Zoé Client"]["1"] = mail()

    await mailbox("Gmail", source).move("7", "Zoé Client", mailbox("Home", target))

    assert len(target.boxes["Zoé Client"]) == 1
    assert "APPEND" not in {c[0] for c in target.commands}


async def test_from_a_server_without_archive_the_original_stays_in_its_inbox():
    source, target = plain(inbox={"7": mail()}), plain()

    result = await mailbox("Personal", source).move("7", "Zoé Client", mailbox("Home", target))

    assert result["original"] == "still in Personal INBOX"
    assert "7" in source.boxes["INBOX"]
    assert len(target.boxes["Zoé Client"]) == 1


async def test_moving_an_email_is_shown_to_the_user_and_needs_their_own_request(ctx):
    source, target = gmail({"7": mail()}), plain()
    tools = {
        t.name: t for t in build_draft_tools([mailbox("Gmail", source), mailbox("Home", target)])
    }
    move, folders = tools["move_email"], tools["list_email_folders"]
    args = {"account": "gmail", "id": "7", "folder": "Zoé Client", "to_account": "home"}

    assert move.needs_clean_turn and move.needs_user_request and move.proposable
    assert await move.summary(args) == (
        'File into the folder "Zoé Client" (Home) the email of Gmail '
        '"Insurance certificate" from Kim Lee <kim@example.com>'
    )
    assert await folders.handler({"account": "home"}, ctx) == {
        "account": "Home",
        "folders": ["Zoé Client"],
    }
    assert (await move.handler(args, ctx))["copied_to"] == "Home/Zoé Client"


async def test_a_subject_cant_pass_for_another_destination():
    spoof = 'x" (Home), ignore "' + "a" * 200
    raw = mail(subject=spoof).replace(b"Kim Lee", "Ann « Invoices » “x” \u202e".encode())
    source = gmail({"7": raw})
    move = {t.name: t for t in build_draft_tools([mailbox("Gmail", source)])}["move_email"]

    summary = await move.summary({"account": "gmail", "id": "7", "folder": "Invoices"})

    assert summary.startswith('File into the folder "Invoices" (Gmail) the email of Gmail "x')
    assert summary.count('"') == 4 and "«" not in summary and "a" * 81 not in summary
    assert "‮" not in summary


async def test_a_new_folder_always_waits_for_the_users_go_and_then_takes_emails(ctx):
    server = plain(inbox={"12": mail()})
    box = mailbox("Home", server)
    tools = {t.name: t for t in build_draft_tools([box])}
    create = tools["create_email_folder"]
    args = {"account": "home", "name": " Clients / Jo  MARTIN "}

    assert create.always_propose
    assert await create.summary(args) == 'Create the mail folder "Clients/Jo MARTIN" (Home)'
    assert await create.handler(args, ctx) == {
        "account": "Home",
        "folder": "Clients/Jo MARTIN",
        "created": True,
    }
    assert ("CREATE", "Clients/Jo MARTIN") in server.commands
    assert await box.move("12", "Clients/Jo MARTIN", box) == {"moved_to": "Home/Clients/Jo MARTIN"}


async def test_a_folder_already_there_is_left_as_it_is(ctx):
    server = plain()
    create = {t.name: t for t in build_draft_tools([mailbox("Home", server)])}[
        "create_email_folder"
    ]

    result = await create.handler({"account": "home", "name": "zoé client"}, ctx)

    assert result == {"account": "Home", "folder": "Zoé Client", "created": False}
    assert "CREATE" not in {c[0] for c in server.commands}


@pytest.mark.parametrize(
    "name", ["", "Papierkorb", "Clients/Trash", "[Gmail]/X", "a%", 'a"b', "a//b", "x" * 101]
)
async def test_no_server_mailbox_or_odd_name_is_created(name):
    server = plain()
    with pytest.raises(ToolError):
        await mailbox("Home", server).create_folder(name)
    assert "CREATE" not in {c[0] for c in server.commands}


def test_a_refused_search_is_an_error_not_a_list_of_uids():
    class Refusing:
        def uid(self, *_args):
            return "NO", [b"[CANNOT] Unknown search key X-GM-RAW"]

    with pytest.raises(imaplib.IMAP4.error):
        search_uids(Refusing(), 'UID 71 X-GM-RAW "-in:sent"')
    assert search_uids(FakeImap([b"12 * 15"]), "ALL") == {b"12", b"15"}


def draft(body="Hello Max,\n\nThe repairs come to [XX].\n\nSam", files=()):
    msg = EmailMessage()
    msg["From"] = "Sam <sam@example.com>"
    msg["To"] = "Max Carter <max@example.com>"
    msg["Cc"] = "kim@example.com"
    msg["Subject"] = "Deposit for Maple Street"
    msg["In-Reply-To"] = "<inventory@example.com>"
    msg.set_content(body)
    for name in files:
        msg.add_attachment(b"%PDF-inv", maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


def with_drafts(server, drafts):
    server.folders["Drafts"] = "\\Drafts"
    server.boxes["Drafts"] = dict(drafts)
    return server


def draft_tools(server):
    return {t.name: t for t in build_draft_tools([mailbox("Home", server)])}


async def test_drafts_are_listed_newest_first_and_taint_the_request(ctx):
    server = with_drafts(plain(), {"5": draft(), "9": draft("Reminder", ["inventory.pdf"])})
    listing = draft_tools(server)["list_email_drafts"]

    result = await listing.handler({"account": "home"}, ctx)

    assert listing.taints
    assert [d["id"] for d in result["drafts"]] == ["9", "5"]
    newest = result["drafts"][0]
    assert newest["to"] == "Max Carter <max@example.com>"
    assert newest["cc"] == "kim@example.com"
    assert newest["attachments"] == [{"name": "inventory.pdf", "type": "application/pdf"}]
    assert "files" not in newest


async def test_a_draft_is_changed_in_place_and_the_old_one_goes_to_the_trash(ctx):
    old = draft(files=["inventory.pdf"])
    server = with_drafts(plain(), {"5": old})
    update = draft_tools(server)["update_email_draft"]
    capture = Attachment("screenshot.png", "image/png", b"\x89PNG-shot", "web")
    ctx.conversation.files[capture.filename] = capture
    args = {
        "account": "home",
        "id": "5",
        "body": "Hello Max,\n\nThe repairs come to 120.50.\n\nSam",
        "attachments": ["screenshot.png"],
    }

    result = await update.handler(args, ctx)

    assert result == {"saved_in": "Home drafts", "old_draft": "in the trash (Deleted Messages)"}
    assert list(server.boxes["Deleted Messages"].values()) == [old]
    (new,) = server.boxes["Drafts"].values()
    message = email.message_from_bytes(new, policy=default_policy)
    assert message["To"] == "Max Carter <max@example.com>"
    assert message["Cc"] == "kim@example.com"
    assert message["Subject"] == "Deposit for Maple Street"
    assert message["In-Reply-To"] == "<inventory@example.com>"
    assert "120.50" in message.get_body().get_content()
    files = {p.get_filename(): p.get_content() for p in message.iter_attachments()}
    assert files == {"inventory.pdf": b"%PDF-inv", "screenshot.png": b"\x89PNG-shot"}
    assert ("APPEND", "Drafts", "(\\Draft \\Seen)") in server.commands


async def test_changing_or_deleting_a_draft_always_waits_for_the_users_go():
    server = with_drafts(plain(), {"5": draft()})
    tools = draft_tools(server)
    update, delete = tools["update_email_draft"], tools["delete_email_draft"]
    args = {
        "account": "home",
        "id": "5",
        "cc": [],
        "body": "Hello Max,\n\nHere is the screenshot.",
        "attachments": ["screenshot.png"],
    }

    assert update.always_propose and delete.always_propose
    assert await update.summary(args) == (
        'Change the draft of Home "Deposit for Maple Street" to Max Carter '
        "<max@example.com>, kim@example.com: cc: nobody ; new text: Hello Max, Here is the "
        "screenshot. ; attach screenshot.png (the old one goes to the trash)"
    )
    assert await delete.summary({"account": "home", "id": "5"}) == (
        'Delete the draft of Home "Deposit for Maple Street" to Max Carter '
        "<max@example.com>, kim@example.com (it goes to the trash)"
    )


async def test_a_deleted_draft_goes_to_the_trash(ctx):
    server = with_drafts(plain(), {"5": draft(), "6": draft("Other")})
    delete = draft_tools(server)["delete_email_draft"]

    result = await delete.handler({"account": "home", "id": "5"}, ctx)

    assert result == {"deleted": True, "in_trash": "Home/Deleted Messages"}
    assert list(server.boxes["Drafts"]) == ["6"]
    assert list(server.boxes["Deleted Messages"].values()) == [draft()]


async def test_a_server_that_cant_take_one_draft_out_keeps_it(ctx):
    server = with_drafts(plain(("IMAP4REV1",)), {"5": draft()})
    tools = draft_tools(server)

    with pytest.raises(ToolError, match="can't put this draft in the trash"):
        await tools["delete_email_draft"].handler({"account": "home", "id": "5"}, ctx)
    result = await tools["update_email_draft"].handler(
        {"account": "home", "id": "5", "subject": "Deposit"}, ctx
    )

    assert "still in the drafts" in result["old_draft"]
    assert len(server.boxes["Drafts"]) == 2
    assert not {"STORE", "EXPUNGE", "MOVE"} & {c[0] for c in server.commands}


@pytest.mark.parametrize(
    "args, error",
    [
        ({"id": "7"}, "No draft with this id"),
        ({"id": "5@INBOX"}, "draft id given by list_email_drafts"),
        ({"id": "5", "remove_attachments": ["quote.pdf"]}, "has no files"),
        ({"id": "5", "to": []}, "a draft needs a recipient"),
        ({"id": "5", "attachments": ["/etc/passwd"]}, "Unknown files"),
    ],
)
async def test_a_draft_change_that_cant_be_done_changes_nothing(ctx, args, error):
    server = with_drafts(plain(), {"5": draft()})

    with pytest.raises(ToolError, match=error):
        await draft_tools(server)["update_email_draft"].handler({"account": "home", **args}, ctx)
    assert list(server.boxes["Drafts"]) == ["5"]
    assert not {"APPEND", "MOVE"} & {c[0] for c in server.commands}


def test_a_parked_draft_change_remembers_the_files_it_attaches(ctx):
    conv = ctx.conversation
    conv.files["screenshot.png"] = Attachment("screenshot.png", "image/png", b"\x89PNG", "web")

    proposal, _ = conv.propose("update_email_draft", {"attachments": ["screenshot.png"]}, "x")

    assert set(proposal.file_hashes) == {"screenshot.png"}


async def test_all_mail_is_never_reached_on_a_server_that_isnt_gmail():
    server = FakeServer(["IMAP4REV1", "MOVE"], {"Archives": "\\All"}, {"7": mail()})
    server.boxes["Archives"] = {"71": dated("<b@x>", "Sent")}
    box = mailbox("Other", server)

    found = await box.recent(datetime.now(UTC) - timedelta(hours=3), False)
    assert all("Archives" not in m.uid for m in found)
    with pytest.raises(ToolError, match="email id given by get_recent_emails"):
        await box.message("71@Archives")


async def test_recent_emails_come_page_by_page(ctx):
    messages = [summary("Personal", f"mail {n}", n) for n in range(30)]
    source = FakeMailbox("Personal", messages)
    recent, *_ = build_email_tools([source], "Europe/Paris")

    first = await recent.handler({"hours": 720, "limit": 20}, ctx)
    second = await recent.handler({"hours": 720, "limit": 20, "offset": 20}, ctx)

    assert [e["subject"] for e in first["emails"]] == [f"mail {n}" for n in range(20)]
    assert first["more"] == "older ones: offset 20"
    assert [e["subject"] for e in second["emails"]] == [f"mail {n}" for n in range(20, 30)]
    assert "more" not in second


async def test_recent_emails_say_they_are_unread_only_unless_asked(ctx):
    source = FakeMailbox("Personal", [summary("Personal", "mail", 0)])
    seen = []

    async def recent_of(since, unread_only, count=25):
        seen.append(unread_only)
        return []

    source.recent = recent_of
    recent, *_ = build_email_tools([source], "Europe/Paris")

    await recent.handler({}, ctx)
    await recent.handler({"unread_only": False}, ctx)

    assert seen == [True, False]
    assert "Only unread ones unless unread_only is false" in recent.description
    assert "default true" in recent.input_schema["properties"]["unread_only"]["description"]


def from_kim(subject, body, message_id):
    msg = EmailMessage()
    msg["From"] = "Kim <kim.lee@example.com>"
    msg["To"] = "sam@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(body)
    return msg.as_bytes()


async def test_emails_are_found_by_sender_in_every_folder_and_can_be_read(ctx):
    server = plain(inbox={"12": dated("<a@x>", "Order")})
    server.boxes["Zoé Client"]["20"] = from_kim(
        "Maple Street", "The warranty of the boiler.", "<b@x>"
    )
    server.boxes["Deleted Messages"]["30"] = from_kim("Photos", "The photos.", "<c@x>")
    _, search, read = build_email_tools([mailbox("Home", server)], "Europe/Paris")

    found = await search.handler({"from": "kim.lee@example.com", "days": 730}, ctx)
    by_text = await search.handler({"text": "warranty"}, ctx)

    assert search.taints
    assert sorted(e["id"] for e in found["emails"]) == ["20@Zoé Client", "30@Deleted Messages"]
    assert [e["subject"] for e in by_text["emails"]] == ["Maple Street"]
    assert 'FROM "kim.lee@example.com"' in server.searches[0]
    message = await read.handler({"account": "home", "id": "20@Zoé Client"}, ctx)
    assert "warranty" in message["text"]


async def test_a_search_asks_each_folder_once_and_reads_unread_from_the_flags(ctx):
    server = plain(inbox={"12": from_kim("Maple Street", "The warranty.", "<a@x>")})
    server.boxes["Zoé Client"]["20"] = from_kim("Boiler", "The warranty too.", "<b@x>")
    server.unseen = {"12"}
    search = build_email_tools([mailbox("Home", server)], "Europe/Paris")[1]

    found = await search.handler({"text": "warranty", "days": 14}, ctx)

    assert {e["id"]: e["unread"] for e in found["emails"]} == {"12": True, "20@Zoé Client": False}
    # A TEXT search over a year of mail takes long: never a second one for the unread.
    # Inbox, the user's folder, the trash: one search each.
    assert len([c for c in server.commands if c[0] == "SEARCH"]) == 3
    assert not any("UNSEEN" in s for s in server.searches)


async def test_unread_only_still_asks_the_server_for_the_unread():
    server = plain(inbox={"12": dated("<a@x>", "Order"), "13": dated("<b@x>", "Read")})
    server.unseen = {"12"}

    found = await mailbox("Home", server).recent(datetime.now(UTC) - timedelta(hours=3), True)

    assert [(m.subject, m.unread) for m in found] == [("Order", True)]
    assert all("UNSEEN" in s for s in server.searches)


def test_flags_are_read_before_or_after_the_emails_bytes():
    data = [
        (b"1 (UID 7 FLAGS (\\Seen \\Flagged) BODY[]<0> {3}", b"one"),
        b")",
        (b"2 (UID 8 BODY[]<0> {3}", b"two"),
        b" FLAGS (\\Seen))",
        (b"3 (UID 9 FLAGS () BODY[]<0> {5}", b"three"),
        b")",
    ]

    assert list(fetched_with_flags(data)) == [
        (b"7", b"one", True),
        (b"8", b"two", True),
        (b"9", b"three", False),
    ]


def test_the_search_tool_asks_for_a_window_the_users_words_give(ctx):
    search = build_email_tools([FakeMailbox("Personal")], "Europe/Paris")[1]

    assert "set days from the user's words" in search.description
    assert "after three searches" in search.description


def test_search_terms_become_imap_keys_with_one_accented_word_sent_last():
    assert search_criteria({"from": "kim", "to": "max@example.com"}) == (
        'FROM "kim" OR TO "max@example.com" CC "max@example.com"',
        "",
    )
    assert search_criteria({"subject": "Résumé  of the day", "text": "rent"}) == (
        'TEXT "rent" SUBJECT',
        "Résumé of the day",
    )


@pytest.mark.parametrize(
    "terms, error",
    [
        ({}, "at least one"),
        ({"from": 'a" OR ALL "'}, "plain words"),
        ({"subject": "Résumé", "text": "café"}, "one search term with accents"),
        ({"to": "zoé@example.com"}, "without accents"),
    ],
)
async def test_a_search_the_server_cant_take_is_refused_before_any_login(ctx, terms, error):
    source = FakeMailbox("Personal")
    search = build_email_tools([source], "Europe/Paris")[1]

    with pytest.raises(ToolError, match=error):
        await search.handler(terms, ctx)
    assert not hasattr(source, "searched")


async def test_an_accented_word_reaches_the_server_in_utf8(ctx):
    server = plain(inbox={"12": from_kim("Résumé of the day", "Here.", "<d@x>")})
    search = build_email_tools([mailbox("Home", server)], "Europe/Paris")[1]

    found = await search.handler({"subject": "Résumé", "from": "kim"}, ctx)

    assert [e["subject"] for e in found["emails"]] == ["Résumé of the day"]
    assert any(
        c[:3] == ("SEARCH", "CHARSET", "UTF-8") and c[3].endswith('FROM "kim" SUBJECT')
        for c in server.commands
    )


class BusyImap:
    """An IMAP server that refuses the first logins as Gmail does past 15 connections."""

    refusals = 0
    refusal = b"[ALERT] Too many simultaneous connections. (Failure)"
    logins = 0
    closed = 0

    def __init__(self, host, port, timeout=None):
        pass

    def login(self, user, password):
        BusyImap.logins += 1
        if BusyImap.logins <= BusyImap.refusals:
            raise imaplib.IMAP4.error(BusyImap.refusal)
        return "OK", [b""]

    def shutdown(self):
        BusyImap.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        BusyImap.closed += 1


@pytest.fixture
def busy_imap(monkeypatch):
    from intelligent_home_agent.tools import mail as mail_module

    monkeypatch.setattr(BusyImap, "logins", 0)
    monkeypatch.setattr(BusyImap, "closed", 0)
    monkeypatch.setattr(mail_module.imaplib, "IMAP4_SSL", BusyImap)
    monkeypatch.setattr(mail_module, "BUSY_WAIT_SECONDS", 0)
    return ImapMailbox(MailAccount(name="Gmail", host="imap.busy", username="u", password="p"))


def test_a_full_server_is_tried_again_after_a_short_wait(busy_imap, monkeypatch):
    monkeypatch.setattr(BusyImap, "refusals", 2)
    with busy_imap._session() as imap:
        assert isinstance(imap, BusyImap)
    assert BusyImap.logins == 3
    assert BusyImap.closed == 3  # the two refused connections and the one used


def test_a_server_still_full_is_reported(busy_imap, monkeypatch):
    monkeypatch.setattr(BusyImap, "refusals", 10)
    with pytest.raises(imaplib.IMAP4.error, match="Too many"), busy_imap._session():
        pass
    assert BusyImap.logins == 3


def test_a_wrong_password_is_not_tried_again(busy_imap, monkeypatch):
    monkeypatch.setattr(BusyImap, "refusals", 10)
    monkeypatch.setattr(BusyImap, "refusal", b"[AUTHENTICATIONFAILED] Invalid credentials")
    with pytest.raises(imaplib.IMAP4.error, match="Invalid"), busy_imap._session():
        pass
    assert BusyImap.logins == 1


def test_one_account_opens_a_few_connections_at_once(busy_imap):
    from intelligent_home_agent.tools.mail import MAX_SESSIONS_PER_ACCOUNT

    open_now, most, lock = 0, 0, threading.Lock()

    def read():
        nonlocal open_now, most
        with busy_imap._session():
            with lock:
                open_now += 1
                most = max(most, open_now)
            time.sleep(0.02)
            with lock:
                open_now -= 1

    threads = [threading.Thread(target=read) for _ in range(MAX_SESSIONS_PER_ACCOUNT * 3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert most == MAX_SESSIONS_PER_ACCOUNT


# --- Any IMAP server: separators, folder names, per-account settings ---------------------


class DottedServer(FakeServer):
    """A server that keeps every folder under the inbox, split by dots, and marks none."""

    def list(self):
        return "OK", [
            f'({flags}) "." "{encode_folder(name)}"'.encode()
            for name, flags in self.folders.items()
        ]


def dotted(inbox=None):
    server = DottedServer(["IMAP4REV1", "MOVE"], inbox=inbox)
    del server.folders["Deleted Messages"], server.boxes["Deleted Messages"]
    for name in ("INBOX.Trash", "INBOX.Sent", "INBOX.Clients"):
        server.folders[name] = "\\HasNoChildren"
        server.boxes[name] = {}
    return server


def test_the_folder_separator_comes_from_the_server():
    folder = parse_folder(b'(\\HasNoChildren) "." "INBOX.Clients.Jo"')
    assert (folder.name, folder.delimiter, folder.files_mail) == ("INBOX.Clients.Jo", ".", True)
    assert parse_folder(b'(\\HasNoChildren) "." "INBOX.Trash"').is_trash
    assert not parse_folder(b'(\\HasNoChildren) "." "INBOX.Trash"').files_mail
    assert not parse_folder(b'(\\HasNoChildren) "." "INBOX.Entw&APw-rfe"').files_mail
    assert parse_folder(b"(\\Noselect) NIL Shared").delimiter == ""


async def test_folders_under_the_inbox_are_filed_into_and_created_with_the_servers_separator(ctx):
    server = dotted(inbox={"12": mail()})
    box = mailbox("Home", server)
    tools = {t.name: t for t in build_draft_tools([box])}

    listed = await tools["list_email_folders"].handler({"account": "home"}, ctx)
    created = await box.create_folder("Clients / Jo")
    again = await box.create_folder("clients/jo")
    moved = await box.move("12", "Clients/Jo", box)

    assert listed["folders"] == ["INBOX.Clients"]
    assert created == {"folder": "INBOX.Clients.Jo", "created": True}
    assert again == {"folder": "INBOX.Clients.Jo", "created": False}
    assert moved == {"moved_to": "Home/INBOX.Clients.Jo"}
    with pytest.raises(ToolError, match="plain words"):
        await box.create_folder("v1.2")  # a dot would make a sub-folder nobody asked for
    with pytest.raises(ToolError, match="never file there"):
        await box.move("12", "Trash", box)


async def test_unmarked_special_folders_are_found_by_their_usual_names():
    server = dotted(inbox={"12": dated("<a@x>", "Order")})
    server.boxes["INBOX.Trash"]["30"] = dated("<b@x>", "Parcel shipped")
    server.boxes["INBOX.Sent"]["40"] = to_max("Boiler", "Sent.", "<c@x>")
    box = mailbox("Home", server)
    since = datetime.now(UTC) - timedelta(hours=3)

    recent = await box.recent(since, unread_only=False)
    sent = await box.search({"to": "max"}, since, 10, received=False)

    assert {m.uid: m.deleted for m in recent} == {"12": False, "30@INBOX.Trash": True}
    assert [(m.uid, m.sent) for m in sent] == [("40@INBOX.Sent", True)]


def configured(server, **settings):
    account = MailAccount(name="Home", host="imap.test", username="u", password="p", **settings)
    box = ImapMailbox(account)
    box._session = lambda timeout=15: nullcontext(server)  # type: ignore[method-assign]
    return box


@pytest.mark.parametrize(
    "folders, setting, expected",
    [
        ({"[Gmail]/Brouillons": "\\Drafts", "Drafts": ""}, None, "[Gmail]/Brouillons"),
        ({"Entwürfe": ""}, None, "Entwürfe"),
        ({"Drafts": "\\Drafts", "To finish": ""}, "to finish", "To finish"),
        ({}, None, "Drafts"),
        ({}, "Unlisted", "Unlisted"),
    ],
)
async def test_drafts_go_to_the_servers_drafts_folder_whatever_its_name(folders, setting, expected):
    server = FakeServer(["IMAP4REV1", "MOVE"], folders)
    server.boxes.setdefault(expected, {})  # there, whether or not the server lists it

    await configured(server, drafts=setting).save_draft(b"Subject: x\r\n\r\ny")

    assert ("APPEND", expected, "(\\Draft \\Seen)") in server.commands


async def test_an_accounts_own_sent_and_trash_folders_win_and_never_take_filed_mail():
    server = FakeServer(["IMAP4REV1", "MOVE"], {"Outgoing": "", "Bin2": "", "Zoé Client": ""})
    server.boxes["INBOX"]["12"] = mail()
    server.boxes["Outgoing"]["40"] = to_max("Boiler", "Sent.", "<c@x>")
    with_drafts(server, {"5": draft()})
    box = configured(server, sent="Outgoing", trash="Bin2")

    sent = await box.search({"to": "max"}, datetime.now(UTC) - timedelta(days=1), 10, False)
    deleted = await box.delete_draft("5")

    assert [m.uid for m in sent] == ["40@Outgoing"]
    assert deleted == {"deleted": True, "in_trash": "Home/Bin2"}
    assert await box.folders() == ["Zoé Client"]
    for folder in ("Outgoing", "Bin2"):
        with pytest.raises(ToolError, match="never file there"):
            await box.move("12", folder, box)


def test_a_draft_is_written_from_the_accounts_address_when_the_login_is_not_one():
    login = MailAccount(name="Home", host="imap.test", username="sam", password="p")
    named = login.model_copy(update={"address": "sam@example.com", "write": True})

    assert ImapMailbox(login).address == "sam" and not ImapMailbox(login).writable
    assert ImapMailbox(named).address == "sam@example.com" and ImapMailbox(named).writable


# --- Through the registry: what waits for the user's go -----------------------------------


async def test_a_flag_after_reading_mail_waits_for_the_users_go_then_runs(ctx):
    server = plain(inbox={"12": mail()})
    box = mailbox("Home", server)
    registry = ToolRegistry(
        [*build_email_tools([box], "Europe/Paris"), *build_draft_tools([box], "en")]
    )
    registry.extend(build_proposal_tools(registry))
    conv = ctx.conversation

    await registry.run("read_email", {"account": "home", "id": "12"}, ctx)
    assert registry.taints("read_email")
    ctx.tainted = True  # what the agent does after a batch that read private content
    parked = await registry.run("flag_email", {"account": "home", "id": "12"}, ctx)
    folder = await registry.run("create_email_folder", {"account": "home", "name": "Taxes"}, ctx)

    assert parked["status"] == folder["status"] == "waiting_for_the_user"
    assert not any(c[0] in {"STORE", "CREATE"} for c in server.commands)
    shown = with_waiting("Here.", ctx.actions, conv)
    assert (
        '1. Flag the email "Insurance certificate" from Kim Lee <kim@example.com> (Home)' in shown
    )
    assert '2. Create the mail folder "Taxes" (Home)' in shown

    conv.turn += 1
    result = await registry.run("run_proposals", {}, ToolContext(conversation=conv))

    assert [d["number"] for d in result["done"]] == [1, 2] and result["failed"] == []
    assert ("STORE", "12", "+FLAGS", "(\\Flagged)") in server.commands
    assert ("CREATE", "Taxes") in server.commands


async def test_moving_an_email_never_runs_by_itself_in_a_scheduled_task(ctx):
    server = plain(inbox={"12": mail()})
    registry = ToolRegistry(build_draft_tools([mailbox("Home", server)]))
    scheduled = ToolContext(conversation=ctx.conversation, scheduled=True)
    args = {"account": "home", "id": "12", "folder": "Zoé Client"}

    try:
        result = await registry.run("move_email", args, scheduled)
    except ToolError:
        result = {"status": "refused"}

    assert result["status"] in {"waiting_for_the_user", "refused"}
    assert "12" in server.boxes["INBOX"]


async def test_a_saved_draft_is_noted_under_the_reply_by_the_code(ctx):
    ctx.conversation.files["kept.pdf"] = Attachment("kept.pdf", "application/pdf", b"%PDF", "email")
    ctx.tainted = True
    tool = build_draft_tools([FakeDrafts("Home")])[1]
    args = {
        "to": ["max@example.com"],
        "subject": "Papers",
        "body": "Hi",
        "attachments": ["kept.pdf"],
    }

    await tool.handler(args, ctx)

    assert with_waiting("Done.", ctx.actions, ctx.conversation).splitlines()[2:] == [
        'Draft saved: max@example.com, "Papers", attached: kept.pdf',
        "Files attached after reading content written by others (kept.pdf): check the "
        "recipients before sending.",
    ]
    french = with_waiting("Fait.", ctx.actions, ctx.conversation, "fr")
    assert "Brouillon enregistré : max@example.com, « Papers », pièces jointes : " in french
