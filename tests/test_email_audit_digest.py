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

import mcp_servers.email_server as email_server  # noqa: E402


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
