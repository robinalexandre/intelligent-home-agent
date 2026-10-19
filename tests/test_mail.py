from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from intelligent_home_agent.tools import ToolError, build_email_tools
from intelligent_home_agent.tools.mail import MailSummary, parse_message, search_uids


def raw_email(subject, body, html=False, date="Tue, 14 Oct 2026 07:12:00 +0200"):
    msg = EmailMessage()
    msg["From"] = "Alex Martin <alex@example.com>"
    msg["Subject"] = subject
    msg["Date"] = date
    if html:
        msg.set_content(
            "<html><head><style>p{}</style></head><body><p>Bonjour Alex,</p>"
            "<p>Peux-tu valider le devis ?</p></body></html>",
            subtype="html",
        )
    else:
        msg.set_content(body)
        msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="devis.pdf")
    return msg.as_bytes()


def test_parse_plain_message_with_attachment():
    summary = parse_message(raw_email("Devis", "Bonjour,\n\nci-joint   le devis."), "Pro", True)
    assert summary.sender == "Alex Martin <alex@example.com>"
    assert summary.subject == "Devis"
    assert summary.snippet == "Bonjour, ci-joint le devis."
    assert summary.received == datetime(2026, 10, 14, 5, 12, tzinfo=UTC)
    assert summary.unread


def test_parse_html_only_message():
    summary = parse_message(raw_email("Devis", "", html=True), "Pro", False)
    assert summary.snippet == "Bonjour Alex, Peux-tu valider le devis ?"


def test_message_without_date_is_skipped():
    assert parse_message(b"Subject: x\r\n\r\nbody", "Pro", False) is None


def test_truncated_message_still_gives_headers():
    raw = raw_email("Facture", "Montant: 1200 euros")[:300]
    summary = parse_message(raw, "Pro", True)
    assert summary.subject == "Facture"


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

    async def recent(self, since, unread_only):
        if self._error:
            raise ToolError(self._error)
        return [m for m in self._messages if m.received >= since]


def summary(account, subject, minutes_ago):
    received = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return MailSummary(account, "x@example.com", subject, received, True, "")


async def test_merges_accounts_newest_first_and_reports_failures(ctx):
    tools = build_email_tools(
        [
            FakeMailbox("Perso", [summary("Perso", "old", 120), summary("Perso", "new", 5)]),
            FakeMailbox("Pro", [summary("Pro", "mid", 60)]),
            FakeMailbox("Gmail", error="Mailbox Gmail unavailable: timeout"),
        ],
        "Europe/Paris",
    )
    (tool,) = tools

    result = await tool.handler({"hours": 24}, ctx)

    assert [e["subject"] for e in result["emails"]] == ["new", "mid", "old"]
    assert result["errors"] == ["Mailbox Gmail unavailable: timeout"]


async def test_no_accounts_means_no_tool():
    assert build_email_tools([], "Europe/Paris") == []
