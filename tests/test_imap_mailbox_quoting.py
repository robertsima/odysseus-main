"""Regression coverage for IMAP mailbox names that contain spaces.

imaplib does not quote mailbox arguments for SELECT/APPEND/MOVE/COPY, so callers
must quote names such as "[Gmail]/All Mail" or "Sent Items" themselves.
"""

from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp_servers.email_server as es


class FakeListConn:
    def __init__(self):
        self.calls = []

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", []

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b""]
        return "OK", []

    def logout(self):
        self.calls.append(("logout",))


class FakeMoveConn:
    def __init__(self):
        self.calls = []

    def list(self):
        self.calls.append(("list",))
        return "OK", []

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", []

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "FETCH":
            return "OK", [b"1 (UID 123)"]
        if command == "MOVE":
            return "NO", []
        return "OK", []

    def expunge(self):
        self.calls.append(("expunge",))

    def logout(self):
        self.calls.append(("logout",))


class FakeGmailListConn:
    """Gmail-style LIST response: Sent lives under [Gmail]/Sent Mail."""

    FOLDERS = [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
        b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
    ]

    def list(self):
        return "OK", list(self.FOLDERS)


def test_mcp_resolve_folder_maps_sent_to_gmail(monkeypatch):
    conn = FakeGmailListConn()
    assert es._resolve_folder(conn, "Sent", "sent") == "[Gmail]/Sent Mail"
    assert es._resolve_folder(conn, "Archive", "archive") == "[Gmail]/All Mail"
    assert es._resolve_folder(conn, "Trash", "trash") == "[Gmail]/Trash"
    assert es._resolve_folder(conn, "Spam", "junk") == "[Gmail]/Spam"
    assert es._resolve_folder(conn, "Drafts", "drafts") == "[Gmail]/Drafts"


def test_mcp_resolve_folder_role_from_name():
    assert es._folder_role_from_name("Sent") == "sent"
    assert es._folder_role_from_name("[Gmail]/Sent Mail") == "sent"
    assert es._folder_role_from_name("[Gmail]/Drafts") == "drafts"


def test_mcp_list_emails_resolves_gmail_sent_before_select(monkeypatch):
    list_conn = FakeGmailListConn()
    select_conn = FakeListConn()

    def _connect(account=None):
        # _list_emails calls _resolve_folder (list) then select on same conn
        class Combo(FakeGmailListConn, FakeListConn):
            def list(self):
                return FakeGmailListConn.list(self)

            def select(self, folder, readonly=False):
                return FakeListConn.select(self, folder, readonly)

            def uid(self, command, *args):
                return FakeListConn.uid(self, command, *args)

            def logout(self):
                FakeListConn.logout(self)

        return Combo()

    monkeypatch.setattr(es, "_imap_connect", _connect)
    assert es._list_emails(folder="Sent", max_results=5) == []
    combo = _connect()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: combo)
    es._list_emails(folder="Sent", max_results=5)
    assert combo.calls[0] == ("select", '"[Gmail]/Sent Mail"', True)


def test_mcp_list_emails_quotes_spaced_folder_on_select(monkeypatch):
    conn = FakeListConn()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)

    assert es._list_emails(folder="Sent Items") == []

    assert conn.calls[0] == ("select", '"Sent Items"', True)


def test_mcp_quote_helper_handles_spaced_and_quoted_mailboxes():
    assert es._q("Sent Items") == '"Sent Items"'
    assert es._q('[Gmail]/All Mail') == '"[Gmail]/All Mail"'
    assert es._q('Label "Needs Reply"') == '"Label \\"Needs Reply\\""'


def test_known_imap_mailbox_call_sites_are_quoted():
    mcp = Path("mcp_servers/email_server.py").read_text()
    assert "conn.select(folder" not in mcp
    assert "conn.select(source_folder" not in mcp
    assert "imap.append(sent_folder" not in mcp
    assert 'conn.uid("MOVE", _b(msg_set), dest_folder)' not in mcp
    assert 'conn.uid("COPY", _b(msg_set), dest_folder)' not in mcp
    assert 'conn.uid("MOVE", _b(uid), dest_folder)' not in mcp
    assert 'conn.uid("COPY", _b(uid), dest_folder)' not in mcp

    pollers = Path("routes/email_pollers.py").read_text()
    assert "conn.select(sent_name" not in pollers
    assert "imap.append(sent_folder" not in pollers

    document_routes = Path("routes/document/document_routes.py").read_text()
    assert "conn.select(doc.source_email_folder" not in document_routes


def test_mcp_move_message_quotes_destination_for_move_and_fallback_copy(monkeypatch):
    conn = FakeMoveConn()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)

    assert es._move_message("123", "INBOX", "[Gmail]/All Mail") is True

    assert ("uid", "MOVE", b"123", '"[Gmail]/All Mail"') in conn.calls
    assert ("uid", "COPY", b"123", '"[Gmail]/All Mail"') in conn.calls


def test_mcp_bulk_move_quotes_destination_for_move_and_fallback_copy(monkeypatch):
    conn = FakeMoveConn()
    monkeypatch.setattr(es, "_imap_connect", lambda account=None: conn)

    assert es._bulk_move(["123"], "INBOX", "[Gmail]/All Mail") == 1

    assert ("uid", "MOVE", b"123", '"[Gmail]/All Mail"') in conn.calls
    assert ("uid", "COPY", b"123", '"[Gmail]/All Mail"') in conn.calls
