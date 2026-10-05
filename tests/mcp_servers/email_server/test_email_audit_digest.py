"""Tests for the audit_emails bulk digest tool (mcp_servers/email_server.py).

audit_emails exists to avoid the context-blowup failure mode where a broad
"go through my emails and categorize X" task calls read_email once per
message, each pulling a full body into the conversation. It should return a
compact digest (bounded snippet per message) instead of full bodies, and
support an optional keyword pre-filter.

Uses the same fixture_email_messages.json mechanism the existing
list_emails/search_emails/read_email fixture tests rely on, so no live IMAP
server is required.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="odysseus-audit-emails-"))
os.environ.setdefault("DATA_DIR", str(_TMP))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'app.db'}")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

import mcp_servers.email_server as email_server  # noqa: E402

_NO_FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="odysseus-audit-emails-empty-"))


@pytest.fixture(autouse=True)
def _leave_fixture_mode_off():
    """Reset DATA_DIR after every test so fixture mode doesn't leak out.

    Pointing email_server.DATA_DIR at a directory containing
    fixture_email_messages.json switches EVERY email tool in the process into
    fixture mode. Left set, it leaks into other test modules (test_imap_*),
    whose fake IMAP connections then never get used because the fixture path
    answers first.
    """
    yield
    email_server.DATA_DIR = _NO_FIXTURE_DIR


def _write_fixture(tmp_path, messages):
    email_server.DATA_DIR = tmp_path
    fixture_path = tmp_path / "fixture_email_messages.json"
    fixture_path.write_text(json.dumps({"messages": messages}), encoding="utf-8")
    return fixture_path


def _sample_messages():
    return [
        {
            "from": "HR Team <hr@acme.example>",
            "subject": "Thanks for applying to Acme",
            "date": "2026-08-01T09:00:00",
            "body": "We have received your application for Software Engineer. " * 20,
        },
        {
            "from": "Recruiter <recruiter@beta.example>",
            "subject": "Interview scheduling for Beta Corp",
            "date": "2026-08-05T09:00:00",
            "body": "We would like to schedule an interview with you next week. " * 20,
        },
        {
            "from": "No Reply <hr@gamma.example>",
            "subject": "Update on your application",
            "date": "2026-08-06T09:00:00",
            "body": "Unfortunately we have decided to move forward with other candidates. " * 20,
        },
        {
            "from": "Newsletter <news@example.com>",
            "subject": "Weekly digest",
            "date": "2026-08-07T09:00:00",
            "body": "Here is your weekly newsletter roundup, nothing job related here. " * 20,
        },
    ]


def test_audit_emails_returns_compact_digest_not_full_bodies(tmp_path):
    _write_fixture(tmp_path, _sample_messages())

    result = email_server._audit_emails(folder="INBOX", limit=10, max_scan=10, snippet_chars=80)

    assert result["success"] is True
    assert result["scanned"] == 4
    assert result["matched"] == 4
    digest = result["digest"]
    assert len(digest) == 4
    for item in digest:
        # The whole point of this tool: never hand back the full body, only
        # a bounded snippet, subject/sender/date/uid for the caller to
        # classify from without blowing up context.
        assert len(item["snippet"]) <= 80
        assert "uid" in item and "subject" in item and "from" in item and "date" in item


def test_audit_emails_keyword_filter_matches_subject_or_body():
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(
        folder="INBOX", keywords=["interview", "unfortunately"], limit=10, max_scan=10,
    )

    assert result["success"] is True
    subjects = {item["subject"] for item in result["digest"]}
    assert subjects == {"Interview scheduling for Beta Corp", "Update on your application"}
    assert result["matched"] == 2
    # scanned reflects everything looked at before filtering, not just matches.
    assert result["scanned"] == 4


def test_audit_emails_keyword_filter_excludes_unrelated_messages():
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(folder="INBOX", keywords=["totally-unmatched-term-xyz"])

    assert result["success"] is True
    assert result["digest"] == []
    assert result["matched"] == 0


def test_audit_emails_no_matching_messages_still_reports_scanned_count():
    _write_fixture(_TMP, [])

    result = email_server._audit_emails(folder="INBOX")

    assert result["success"] is True
    assert result["digest"] == []
    assert result["scanned"] == 0


def test_audit_emails_limit_and_max_scan_are_bounded():
    # Absurd caller-supplied values must be clamped rather than trusted
    # directly into an IMAP fetch / unbounded snippet allocation. Clamping
    # happens before the fixture dispatch so both the fixture and real IMAP
    # paths get the same bounds.
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(
        folder="INBOX", limit=10_000, max_scan=10_000, snippet_chars=10_000,
    )

    assert result["success"] is True
    assert len(result["digest"]) <= 100
    for item in result["digest"]:
        assert len(item["snippet"]) <= 1000


def test_audit_emails_fixture_disabled_returns_none_from_fixture_helper():
    # When there's no fixture file, the fixture helper must return None so
    # _audit_emails falls through to the real IMAP path rather than
    # silently reporting an empty mailbox.
    empty_dir = Path(tempfile.mkdtemp(prefix="odysseus-audit-emails-empty-"))
    email_server.DATA_DIR = empty_dir

    assert email_server._fixture_audit_emails(folder="INBOX") is None


# ── aggregation: report totals without enumerating rows ──────────────────────
#
# The failure this covers: asked to build a rolling job-application report over
# an 11,688-message Gmail INBOX, the agent paged list_emails ten at a time for
# 13 rounds and still had no total. Counting has to happen server-side; the
# model writes prose over the aggregates and cites a few sample rows.


def test_audit_emails_summary_counts_every_match_not_just_the_digest():
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(folder="INBOX", limit=1, max_scan=10)

    assert len(result["digest"]) == 1, "digest is capped by limit"
    summary = result["summary"]
    assert summary["total_matched"] == 4, "aggregates cover all matches, not the capped digest"
    assert result["truncated"] is True
    assert summary["unique_senders"] == 4
    domains = {b["domain"]: b["count"] for b in summary["by_sender_domain"]}
    assert domains == {
        "acme.example": 1, "beta.example": 1, "gamma.example": 1, "example.com": 1,
    }
    months = {b["month"]: b["count"] for b in summary["by_month"]}
    assert months == {"2026-08": 4}, "dates must bucket, not fall into (unknown)"
    assert summary["earliest"] == "2026-08-01"
    assert summary["latest"] == "2026-08-07"


def test_audit_emails_summary_buckets_by_keyword():
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(
        folder="INBOX", keywords=["application", "interview"], limit=30,
    )

    buckets = {b["keyword"]: b["count"] for b in result["summary"]["by_keyword"]}
    assert buckets["application"] == 2
    assert buckets["interview"] == 1


def test_audit_emails_summarize_shrinks_digest_and_keeps_totals():
    _write_fixture(_TMP, _sample_messages() * 6)

    result = email_server._audit_emails(folder="INBOX", limit=100, summarize=True)

    assert result["matched"] == 24
    assert result["summary"]["total_matched"] == 24
    assert len(result["digest"]) == 10, "summarize mode returns a small sample only"
    assert result["truncated"] is True


def test_audit_emails_result_keeps_its_old_shape_for_existing_callers():
    # The new `summary`/`search`/`truncated` fields are additive: a caller that
    # only reads digest/scanned/matched/folder must keep working unchanged.
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(folder="INBOX", limit=10, max_scan=10)

    assert {"success", "digest", "scanned", "matched", "folder", "account"} <= set(result)
    assert result["scanned"] == 4 and result["matched"] == 4


def test_audit_emails_date_bounds_filter_the_fixture_mailbox():
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(
        folder="INBOX", since="2026-08-05", before="2026-08-07",
    )

    subjects = {item["subject"] for item in result["digest"]}
    assert subjects == {"Interview scheduling for Beta Corp", "Update on your application"}


def test_audit_emails_ambiguous_date_is_rejected_not_guessed():
    # 03/04/2026 could be March 4th or April 3rd; guessing would quietly return
    # the wrong month's mail inside a report the user then trusts.
    assert email_server._parse_audit_date("03/04/2026") is None
    assert email_server._parse_audit_date("2026-08-01").month == 8
    assert email_server._parse_audit_date("01-Aug-2026").month == 8
    assert email_server._parse_audit_date("") is None


def test_audit_emails_snippet_chars_zero_skips_bodies_entirely():
    _write_fixture(_TMP, _sample_messages())

    result = email_server._audit_emails(folder="INBOX", snippet_chars=0)

    assert result["matched"] == 4
    assert all(item["snippet"] == "" for item in result["digest"])


# ── server-side search over a real (faked) IMAP connection ───────────────────


def _raw_message(subject, sender, date_str, body):
    return (
        f"From: {sender}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date_str}\r\n"
        f"Message-ID: <{abs(hash(subject)) % 10**8}@example.invalid>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


class _FakeIMAP:
    """Minimal IMAP stand-in that records the SEARCH/FETCH commands issued.

    The point of these tests is *which command goes over the wire* -- whether
    the narrowing happens on the server or by downloading the mailbox -- so the
    fake records commands rather than simulating a mail store.
    """

    def __init__(self, messages, capabilities=("IMAP4REV1", "UIDPLUS"), search_handler=None):
        self.messages = messages            # {uid:int -> raw bytes}
        self.capabilities = capabilities
        self.searches = []                  # list of the args tuples
        self.fetches = []                   # list of (uids, spec)
        self._search_handler = search_handler
        self.logged_out = False

    def select(self, mailbox, readonly=False):
        return ("OK", [b"4"])

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args)
            if self._search_handler is not None:
                handled = self._search_handler(args)
                if handled is not None:
                    return handled
            return ("OK", [b" ".join(str(u).encode() for u in sorted(self.messages))])
        if command == "FETCH":
            uid_arg, spec = args[0], args[1]
            uids = [int(u) for u in uid_arg.decode().split(",") if u]
            self.fetches.append((uids, spec))
            header_only = "HEADER.FIELDS" in spec
            out = []
            for u in uids:
                raw = self.messages.get(u)
                if raw is None:
                    continue
                payload = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if header_only else raw
                out.append((f"1 (UID {u} RFC822 {{{len(payload)}}}".encode(), payload))
            return ("OK", out)
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.logged_out = True

    # Guard the codebase-wide rule that email code never uses sequence numbers.
    def search(self, *a, **k):
        raise AssertionError("audit_emails must use conn.uid('SEARCH', ...)")

    def fetch(self, *a, **k):
        raise AssertionError("audit_emails must use conn.uid('FETCH', ...)")


def _install_fake_imap(monkeypatch, conn):
    empty_dir = Path(tempfile.mkdtemp(prefix="odysseus-audit-emails-live-"))
    monkeypatch.setattr(email_server, "DATA_DIR", empty_dir)   # fixture mode off
    monkeypatch.setattr(email_server, "_imap_connect", lambda account=None: conn)
    monkeypatch.setattr(email_server, "_resolve_folder", lambda c, folder, role=None: folder)
    return conn


def _fake_inbox(count=4, subject="Thanks for applying to Acme"):
    return {
        uid: _raw_message(
            f"{subject} #{uid}",
            f"HR Team <hr{uid}@acme.example>",
            "Tue, 04 Aug 2026 09:00:00 +0000",
            "We have received your application. " * 30,
        )
        for uid in range(1, count + 1)
    }


def test_gmail_account_search_uses_x_gm_raw_over_the_whole_mailbox(monkeypatch):
    conn = _install_fake_imap(
        monkeypatch,
        _FakeIMAP(_fake_inbox(), capabilities=("IMAP4REV1", "X-GM-EXT-1")),
    )

    result = email_server._audit_emails(
        folder="INBOX",
        query="subject:(application OR applying) OR from:linkedin.com",
        since="2026-01-01",
        limit=5,
    )

    assert result["success"] is True
    assert conn.searches, "no SEARCH was issued"
    args = conn.searches[0]
    assert args[0] == "X-GM-RAW", "Gmail accounts must search server-side, not scan the newest N"
    assert "subject:(application OR applying) OR from:linkedin.com" in args[1]
    assert "after:2026/01/01" in args[1]
    assert result["search"]["mode"] == "gmail_raw"


def test_non_gmail_account_builds_standard_imap_criteria(monkeypatch):
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox()))

    result = email_server._audit_emails(
        folder="INBOX", keywords=["application", "interview"],
        since="2026-01-01", before="2026-09-01", limit=5,
    )

    criteria = conn.searches[0][1]
    assert conn.searches[0][0] is None
    assert "SINCE 01-Jan-2026" in criteria
    assert "BEFORE 01-Sep-2026" in criteria
    assert 'SUBJECT "application"' in criteria and 'BODY "application"' in criteria
    assert criteria.startswith("SINCE") and "OR" in criteria
    assert result["search"]["mode"] == "imap_criteria"


def test_rejected_server_criteria_fall_back_to_all_with_client_side_filter(monkeypatch):
    # Some IMAP servers mishandle BODY/OR chains (email_pollers.py documents the
    # same unreliability for SINCE). A rejected criteria must not lose the
    # request -- degrade to ALL and keep the old client-side keyword filter.
    def handler(args):
        if args[0] is None and args[1] != "ALL":
            return ("NO", [b""])
        return None

    inbox = _fake_inbox(2)
    inbox[3] = _raw_message(
        "Weekly digest", "News <news@example.com>",
        "Tue, 04 Aug 2026 09:00:00 +0000", "nothing job related here",
    )
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(inbox, search_handler=handler))

    result = email_server._audit_emails(folder="INBOX", keywords=["application"], limit=10)

    assert result["search"]["mode"] == "all"
    assert result["search"]["fallback_reason"]
    assert result["matched"] == 2, "client-side keyword filter still has to run"
    assert all("applying" in item["subject"] for item in result["digest"])


def test_search_terms_cannot_inject_an_imap_command(monkeypatch):
    # Keywords and query are model-authored. imaplib appends command arguments
    # verbatim, so a raw CRLF would end the command line and run what follows.
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox()))

    email_server._audit_emails(
        folder="INBOX",
        keywords=['appl"ication\r\nA001 LOGOUT'],
        limit=5,
    )

    criteria = conn.searches[0][1]
    assert "\r" not in criteria and "\n" not in criteria
    assert "A001 LOGOUT" not in criteria.replace("\\", "")
    assert '\\"' in criteria, "an embedded quote must be escaped, not left to break the string"


def test_non_ascii_keyword_drops_the_whole_server_term_clause(monkeypatch):
    # An IMAP quoted string without CHARSET must be ASCII. Sending only the
    # expressible subset of the keywords would silently under-match, so the
    # term clause is dropped wholesale and the client-side filter takes over.
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox()))

    email_server._audit_emails(
        folder="INBOX", keywords=["application", "candidatúra"], since="2026-01-01", limit=5,
    )

    criteria = conn.searches[0][1]
    assert "SINCE 01-Jan-2026" in criteria
    assert "SUBJECT" not in criteria, "a partial term clause would under-match"


def test_server_narrowed_scan_reaches_past_the_unfiltered_250_ceiling(monkeypatch):
    conn = _install_fake_imap(
        monkeypatch,
        _FakeIMAP(_fake_inbox(400), capabilities=("IMAP4REV1", "X-GM-EXT-1")),
    )

    result = email_server._audit_emails(
        folder="INBOX", query="subject:application", max_scan=1000, limit=5,
    )

    assert result["scanned"] == 400, (
        "a server-narrowed UID set must not be truncated at the unfiltered ceiling"
    )
    assert result["summary"]["total_matched"] == 400


def test_unfiltered_scan_keeps_the_old_250_ceiling(monkeypatch):
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox(400)))

    result = email_server._audit_emails(folder="INBOX", max_scan=1000, limit=5)

    assert result["search"]["mode"] == "all"
    assert result["scanned"] == 250


def test_header_only_pass_fetches_bodies_only_for_returned_rows(monkeypatch):
    # The old implementation fetched (UID RFC822) for every candidate just to
    # build a 300-char snippet. Headers are peeked for all of them; bodies are
    # pulled only for the rows that end up in the digest.
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox(40)))

    result = email_server._audit_emails(folder="INBOX", limit=5, max_scan=40)

    header_fetches = [f for f in conn.fetches if "HEADER.FIELDS" in f[1]]
    body_fetches = [f for f in conn.fetches if "HEADER.FIELDS" not in f[1]]
    assert header_fetches and len(header_fetches[0][0]) == 40
    assert "BODY.PEEK" in header_fetches[0][1], "a bare BODY[] fetch would mark mail read"
    assert sum(len(f[0]) for f in body_fetches) == 5
    assert all(item["snippet"] for item in result["digest"])


def test_counts_only_pass_fetches_no_bodies_at_all(monkeypatch):
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox(40)))

    email_server._audit_emails(folder="INBOX", limit=5, max_scan=40, snippet_chars=0)

    assert all("HEADER.FIELDS" in spec for _, spec in conn.fetches)


def test_gmail_detection_uses_capability_then_host(monkeypatch):
    assert email_server._is_gmail_account(_FakeIMAP({}, capabilities=("X-GM-EXT-1",))) is True

    plain = _FakeIMAP({}, capabilities=("IMAP4REV1",))
    monkeypatch.setattr(email_server, "_load_config", lambda a=None: {"imap_host": "imap.gmail.com"})
    assert email_server._is_gmail_account(plain) is True
    monkeypatch.setattr(email_server, "_load_config", lambda a=None: {"imap_host": "mail.dovecot.local"})
    assert email_server._is_gmail_account(plain) is False


def test_audit_emails_always_logs_out_even_on_the_search_path(monkeypatch):
    conn = _install_fake_imap(monkeypatch, _FakeIMAP(_fake_inbox(3)))

    email_server._audit_emails(folder="INBOX", limit=2)

    assert conn.logged_out is True
