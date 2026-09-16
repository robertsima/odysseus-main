"""What one inbox poll costs the IMAP server.

The production trace this pins down: `folder=INBOX filter=unread limit=10
offset=0` against an 11,823-message mailbox, re-issued by the inbox widget
once a minute, taking 1.1s-5.9s *every* time. Two things made it repeat in
full: `_resolve_mail_folder` re-LISTed every mailbox on each request, and the
list cache's TTL was shorter than the poll interval, so it was written on
every request and read on none.

These tests count IMAP round trips against a faked client rather than timing
anything, so they stay meaningful without a live server.
"""

import asyncio
import time

import pytest


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _header(uid: str) -> bytes:
    return (
        f"From: Sender {uid} <s{uid}@example.com>\r\n"
        f"To: admin@example.com\r\n"
        f"Subject: Message {uid}\r\n"
        f"Message-ID: <m{uid}@example.com>\r\n"
        "Date: Tue, 01 Sep 2026 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    ).encode()


class FakeImap:
    """Enough of imaplib.IMAP4 for the list path, with a shared command log."""

    def __init__(self, calls, uids, folders=("INBOX", "Sent", "[Gmail]/Sent Mail")):
        self.calls = calls
        self.uids = uids
        self.folders = list(folders)

    def noop(self):
        self.calls.append(("NOOP", ""))
        return "OK", [b"NOOP done"]

    def list(self, *_a, **_k):
        self.calls.append(("LIST", ""))
        return "OK", [
            f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders
        ]

    def select(self, mailbox, readonly=False):
        name = mailbox.decode() if isinstance(mailbox, bytes) else str(mailbox)
        self.calls.append(("SELECT", name.strip('"')))
        return "OK", [str(len(self.uids)).encode()]

    def uid(self, command, arg, query=None):
        if command == "SEARCH":
            self.calls.append(("SEARCH", query))
            return "OK", [" ".join(self.uids).encode()]
        self.calls.append(("FETCH", ""))
        wanted = (arg.decode() if isinstance(arg, bytes) else str(arg)).split(",")
        out = []
        for i, uid in enumerate(wanted, start=1):
            raw = _header(uid)
            out.append((
                b"%d (UID %s FLAGS () RFC822.SIZE 4096 RFC822.HEADER {%d}"
                % (i, uid.encode(), len(raw)),
                raw,
            ))
            out.append(b")")
        return "OK", out

    def logout(self):
        self.calls.append(("LOGOUT", ""))
        return "BYE", []


@pytest.fixture
def email_env(tmp_path, monkeypatch):
    """A router wired to a faked IMAP client, plus the shared command log."""
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()
    # Firing `email_received` kicks off an away-reply pass that wants an LLM;
    # the event itself is covered directly by the last test in this file.
    monkeypatch.setattr(email_routes, "_record_email_received_events", lambda *a, **k: None)

    calls: list[tuple[str, str]] = []
    uids = [str(n) for n in range(1, 11824)]
    connects = []

    def fake_connect(account_id=None, owner="", timeout=None):
        connects.append((account_id, owner))
        return FakeImap(calls, uids)

    monkeypatch.setattr(email_routes, "_imap_connect", fake_connect)
    monkeypatch.setattr(email_helpers, "_imap_connect", fake_connect)

    router = email_routes.setup_email_routes()
    # The first request would otherwise start the real background pollers.
    monkeypatch.setattr(email_routes._start_poller, "_deferred", None, raising=False)

    endpoint = _route_endpoint(router, "/api/email/list", "GET")

    async def poll(**kw):
        params = dict(
            folder="INBOX", limit=10, offset=0, filter="unread", from_addr=None,
            account_id=None, has_attachments=0, cached_only=0, cache_bust=None,
            owner="admin",
        )
        params.update(kw)
        return await endpoint(**params)

    return {"calls": calls, "uids": uids, "connects": connects, "poll": poll}


def _commands(calls):
    return [c for c, _ in calls]


def _advance_clock(monkeypatch, seconds):
    """Age every monotonic deadline in the route closure without sleeping."""
    base = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: base() + seconds)


@pytest.mark.asyncio
async def test_inbox_poll_never_lists_mailboxes(email_env):
    """INBOX is reserved by RFC 3501, so the LIST that used to precede every
    SELECT told us nothing and cost a full mailbox enumeration per poll."""
    result = await email_env["poll"]()

    assert len(result["emails"]) == 10
    assert result["total"] == 11823
    assert "LIST" not in _commands(email_env["calls"])
    assert _commands(email_env["calls"]).count("SEARCH") == 1


@pytest.mark.asyncio
async def test_non_inbox_folder_is_resolved_once_per_account(email_env):
    poll = email_env["poll"]
    calls = email_env["calls"]

    await poll(folder="Sent")
    assert _commands(calls).count("LIST") == 1

    calls.clear()
    await poll(folder="Sent", cache_bust="1")
    assert "LIST" not in _commands(calls)

    # Multi-account safety: a memoised name belongs to one account+owner and
    # must not stand in for another mailbox's folder layout.
    calls.clear()
    await poll(folder="Sent", account_id="other", cache_bust="2")
    assert _commands(calls).count("LIST") == 1

    calls.clear()
    await poll(folder="Sent", owner="bob", cache_bust="3")
    assert _commands(calls).count("LIST") == 1


@pytest.mark.asyncio
async def test_repeat_poll_is_served_from_memory(email_env):
    """The second poll must not wait on IMAP at all — that is the whole point
    of the cache, and with the old 45s TTL against a 60s poll it never did."""
    poll = email_env["poll"]
    calls = email_env["calls"]

    first = await poll()
    assert first["sync"]["source"] == "imap"

    calls.clear()
    second = await poll()

    assert second["sync"]["source"] == "memory_cache"
    assert second["emails"] == first["emails"]
    assert second["total"] == first["total"]
    assert calls == []


@pytest.mark.asyncio
async def test_stale_entry_is_served_immediately_and_refreshed_behind_it(email_env, monkeypatch):
    poll = email_env["poll"]
    calls = email_env["calls"]

    await poll()
    calls.clear()

    # A minute later — the cadence of the real poll — plus new mail that landed
    # in the meantime.
    _advance_clock(monkeypatch, 60)
    email_env["uids"].append("11824")

    result = await poll()
    assert result["sync"]["source"] == "memory_cache"
    assert result["sync"]["revalidating"] is True
    assert calls == [], "a stale hit must not block on IMAP"

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=5)

    assert "SEARCH" in _commands(calls), "the refresh must still re-ask IMAP"
    # No new handshake: the refresh rides the connection the first poll pooled.
    assert len(email_env["connects"]) == 1

    refreshed = await poll()
    assert refreshed["total"] == 11824
    assert refreshed["emails"][0]["uid"] == "11824"


@pytest.mark.asyncio
async def test_cached_pages_are_not_shared_across_owners_or_accounts(email_env):
    poll = email_env["poll"]

    await poll(owner="alice")
    for scope in ({"owner": "bob"}, {"owner": "alice", "account_id": "work"}):
        result = await poll(**scope)
        assert result["sync"]["source"] == "imap", f"{scope} must not reuse another scope's page"


@pytest.mark.asyncio
async def test_email_received_fires_for_new_mail_after_the_first_baseline(tmp_path, monkeypatch):
    """`_record_email_received_events` moved off a whole-table COUNT(*); the
    baseline semantics it encodes must not move with it."""
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes
    import src.event_bus as event_bus

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    monkeypatch.setattr(email_routes, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    fired = []
    monkeypatch.setattr(event_bus, "fire_event", lambda name, owner: fired.append((name, owner)))

    first_page = [{"message_id": f"<m{n}@example.com>", "uid": str(n)} for n in range(1, 11)]
    email_routes._record_email_received_events("admin", None, "INBOX", first_page)
    assert fired == [], "the first page seen is a baseline, not ten new arrivals"

    email_routes._record_email_received_events("admin", None, "INBOX", first_page)
    assert fired == []

    arrival = [{"message_id": "<new@example.com>", "uid": "11"}] + first_page
    email_routes._record_email_received_events("admin", None, "INBOX", arrival)
    assert fired == [("email_received", "admin")]
