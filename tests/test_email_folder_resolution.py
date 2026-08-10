"""Gmail-style IMAP folder name resolution in the HTTP email routes."""

import routes.email_routes as email_routes


class FakeGmailListConn:
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


def test_resolve_mail_folder_maps_sent_to_gmail():
    conn = FakeGmailListConn()
    assert email_routes._resolve_mail_folder(conn, "Sent", "sent") == "[Gmail]/Sent Mail"
    assert email_routes._resolve_mail_folder(conn, "Archive", "archive") == "[Gmail]/All Mail"
    assert email_routes._resolve_mail_folder(conn, "Drafts", "drafts") == "[Gmail]/Drafts"


def test_folder_role_from_name_detects_sent_and_drafts():
    assert email_routes._folder_role_from_name("Sent") == "sent"
    assert email_routes._folder_role_from_name("[Gmail]/Sent Mail") == "sent"
    assert email_routes._folder_role_from_name("[Gmail]/Drafts") == "drafts"
