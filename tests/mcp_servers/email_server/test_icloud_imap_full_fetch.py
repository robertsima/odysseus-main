"""Regression for issue #1961: read_email, reply_to_email and download_attachment
failed on iCloud IMAP accounts.

iCloud's IMAP server silently ignores the legacy bare `RFC822` fetch item: a
`UID FETCH <uid> (RFC822)` returns status OK but only `(UID <uid>)` with no body
tuple, so the message looks "not found" even though list_emails works (it uses
`RFC822.HEADER`, which iCloud honours). The modern `BODY.PEEK[]` item is honoured
by iCloud and Gmail alike and doesn't set \\Seen.

The three tools run here against a connection that answers like iCloud does.
"""
import pytest

pytest.importorskip("mcp")

import mcp_servers.email_server as es

RAW = (
    b"From: Bob <bob@example.com>\r\nTo: me@example.com\r\nSubject: Quarterly plan\r\n"
    b"Message-ID: <icloud-1@example.com>\r\nMIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
    b"--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlease review the plan.\r\n"
    b"--XX\r\nContent-Type: text/plain; name=\"notes.txt\"\r\n"
    b"Content-Disposition: attachment; filename=\"notes.txt\"\r\n\r\nattachment text\r\n"
    b"--XX--\r\n"
)


class ICloudConnection:
    """Answers full-message fetches the way iCloud does: only BODY.PEEK[] returns the body."""

    def __init__(self):
        self.fetch_items = []

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"7"]
        if command != "FETCH":
            return "OK", [None]
        item = args[-1]
        self.fetch_items.append(item)
        if item == "(BODY.PEEK[])":
            return "OK", [(b"1 (UID 7 BODY[] {1}", RAW), b")"]
        if "RFC822.HEADER" in item:
            headers = RAW.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            return "OK", [(b"1 (UID 7 RFC822.HEADER {1}", headers), b")"]
        return "OK", [b"1 (UID 7)"]  # bare RFC822: no body tuple

    def logout(self):
        pass


@pytest.fixture
def icloud(monkeypatch, tmp_path):
    conn = ICloudConnection()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)
    monkeypatch.setattr(es, "_load_config", lambda account=None: {"imap_user": "me@example.com", "cache_db": str(tmp_path / "mail.db")})
    monkeypatch.setattr(es, "MAIL_ATTACHMENTS_DIR", str(tmp_path))
    return conn


def test_read_email_returns_the_message_from_an_icloud_style_server(icloud):
    result = es._read_email(uid="7")

    assert result.get("subject") == "Quarterly plan", result
    assert "Please review the plan." in result["body"]


def test_reply_to_email_finds_the_original_on_an_icloud_style_server(icloud, monkeypatch):
    sent = {}
    monkeypatch.setattr(es, "_send_email", lambda **kwargs: sent.update(kwargs) or {"status": "sent"})

    es._reply_to_email("7", "Thanks, will do.")

    assert sent.get("to") == "bob@example.com"
    assert sent.get("subject") == "Re: Quarterly plan"


def test_download_attachment_finds_the_message_on_an_icloud_style_server(icloud):
    result = es._download_attachment("7", 0)

    assert "error" not in result, result
    assert result["filename"] == "notes.txt"


def test_listing_still_works_on_an_icloud_style_server(icloud):
    emails = es._list_emails(folder="INBOX")

    assert [e["subject"] for e in emails] == ["Quarterly plan"]
