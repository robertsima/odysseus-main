"""Clearing reminder emails finds the ones sent before the Agamemnon rename.

Reminder emails are now titled "Reminder (Agamemnon): …". Clear (and the
Reminders filter) used to search only for "Reminder (Odysseus): …", so after
the rename every reminder already in a mailbox would have stayed behind.
"""
import re

import routes.email_routes as email_routes

MAILBOX = {
    b"1": "Reminder (Odysseus): Renew passport",
    b"2": "Reminder (Agamemnon): Submit report",
    b"3": "Weekly newsletter",
}


class FakeImap:
    """INBOX only; SEARCH matches on SUBJECT, the way an IMAP server would."""

    def __init__(self):
        self.deleted = set()

    def select(self, name):
        return ("OK", [b"3"]) if name.strip('"') == "INBOX" else ("NO", [])

    def uid(self, command, *args):
        if command == "SEARCH":
            wanted = re.search(r'SUBJECT "([^"]*)"', args[-1])
            hits = [uid for uid, subject in MAILBOX.items() if wanted and wanted.group(1) in subject]
            return "OK", [b" ".join(hits)]
        if command == "STORE":
            self.deleted.add(args[0])
        return "OK", [b""]

    def expunge(self):
        return "OK", []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_clear_removes_reminders_from_before_and_after_the_rename(api, monkeypatch):
    conn = FakeImap()
    monkeypatch.setattr(email_routes, "_imap", lambda *a, **kw: conn)
    monkeypatch.setattr(email_routes, "_get_email_config",
                        lambda *a, **kw: {"from_address": "alice@example.edu"})
    monkeypatch.setattr(email_routes, "_detect_sent_folder", lambda c: "Sent")

    response = api.as_user("alice").delete("/api/email/odysseus/reminders")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert conn.deleted == {b"1", b"2"}
