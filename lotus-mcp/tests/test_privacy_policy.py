"""Server-side consent enforcement.

The theme of this module: a client argument may narrow access but never widen
it, and the enforcement lives in the server rather than in a prompt.
"""

from __future__ import annotations

import json
import logging
import socket
from datetime import UTC, datetime, timedelta

import pytest

from lotus_mcp.config import PrivacyPolicySettings
from lotus_mcp.models import SourceType
from lotus_mcp.policy import PolicyEngine, PolicyError, install_network_guard
from lotus_mcp.server import call_tool_sync

from .conftest import csv_document, csv_row

START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 10, tzinfo=UTC)
SECRET_NOTE = "SYNTHETIC-JOURNAL-MARKER"


def test_missing_env_config_falls_back_to_the_strictest_settings(tmp_path, monkeypatch):
    """The image sets $LOTUS_CONFIG before any file is mounted there.

    Falling back is only acceptable because the fallback is the most
    restrictive setting available — a missing file can never grant more access.
    """
    from lotus_mcp.config import ENV_CONFIG_PATH, load_config

    monkeypatch.setenv(ENV_CONFIG_PATH, str(tmp_path / "absent.yaml"))
    config = load_config()
    assert config.privacy.expose_raw_entries is False
    assert config.privacy.import_notes is False


def test_explicitly_passed_config_path_must_exist(tmp_path):
    """A caller-supplied path is strict: a typo is an error, not a fallback."""
    from lotus_mcp.config import load_config

    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "typo.yaml")


def test_shipped_defaults_are_conservative():
    policy = PrivacyPolicySettings()
    assert policy.expose_raw_entries is False
    assert policy.expose_notes is False
    assert policy.include_note_themes is False
    assert policy.import_notes is False
    assert policy.allow_cross_domain_correlation is False
    assert policy.allow_writeback_to_source is False
    assert policy.allow_external_network_calls is False
    assert policy.max_query_days_without_confirmation == 90
    assert policy.maximum_entry_result_limit == 500


def test_raw_entry_search_is_denied_by_default(ctx):
    with pytest.raises(PolicyError, match="Raw mood entries are not exposed"):
        ctx.queries.search_entries(start=START, end=END)


def test_client_cannot_enable_notes_by_asking(make_context):
    """include_notes: true is refused, not quietly downgraded."""
    ctx = make_context(privacy={"expose_raw_entries": True})
    with pytest.raises(PolicyError, match="Journal notes are not exposed"):
        ctx.queries.search_entries(start=START, end=END, include_notes=True)


def test_notes_are_absent_by_default_even_when_stored(make_context, drop):
    ctx = make_context(
        privacy={"import_notes": True, "expose_raw_entries": True, "expose_notes": False}
    )
    drop("notes.csv", csv_document(csv_row("n1", note=SECRET_NOTE)))
    ctx.imports.import_file(
        "notes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )
    result = ctx.queries.search_entries(start=START, end=END)
    assert result["notes_included"] is False
    assert SECRET_NOTE not in json.dumps(result)
    assert "note" not in result["entries"][0]


def test_note_import_is_refused_when_policy_forbids_it(ctx, drop):
    drop("notes.csv", csv_document(csv_row("n1", note=SECRET_NOTE)))
    with pytest.raises(PolicyError, match="Importing journal notes is disabled"):
        ctx.imports.import_file(
            "notes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
        )


def test_notes_are_not_read_off_disk_by_default(ctx, drop):
    """Default import stores everything except the note column."""
    drop("notes.csv", csv_document(csv_row("n1", note=SECRET_NOTE)))
    report = ctx.imports.import_file("notes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    assert report.inserted_count == 1
    assert report.notes_imported is False
    assert ctx.database.counts()["entries_with_notes"] == 0

    conn = ctx.database.connect()
    try:
        row = conn.execute("SELECT note, note_hash FROM mood_entries").fetchone()
    finally:
        conn.close()
    assert row["note"] is None
    assert row["note_hash"] == ""


def test_note_themes_are_denied_by_policy(ctx):
    with pytest.raises(PolicyError, match="Note-derived themes are disabled"):
        ctx.summaries.summarize_period(start=START, end=END, include_note_themes=True)


def test_note_themes_stay_unimplemented_even_when_permitted(make_context):
    ctx = make_context(privacy={"expose_notes": True, "include_note_themes": True})
    result = ctx.summaries.summarize_period(start=START, end=END, include_note_themes=True)
    # Permitted, but the local extractor is a designed extension point, not a
    # silent hand-off to something else.
    assert result["note_themes"] is None
    assert result["note_themes_status"] == "not_implemented"


def test_aggregate_summaries_can_be_disabled(make_context):
    ctx = make_context(privacy={"expose_aggregate_summaries": False})
    with pytest.raises(PolicyError, match="Aggregate summaries are disabled"):
        ctx.summaries.summarize_period(start=START, end=END)


def test_emotion_labels_can_be_withheld_from_summaries(make_context, drop):
    ctx = make_context(privacy={"expose_emotion_labels": False})
    drop("a.csv", csv_document(csv_row("a1")))
    ctx.imports.import_file("a.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    result = ctx.summaries.summarize_period(start=START, end=END)
    assert "emotion_counts" not in result
    assert result["total_entries"] == 1


def test_range_limit_requires_confirmation():
    policy = PolicyEngine(PrivacyPolicySettings())
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(PolicyError, match="above the 90-day limit"):
        policy.validate_range(start, start + timedelta(days=120))
    # Explicit acknowledgement is accepted.
    policy.validate_range(start, start + timedelta(days=120), confirmed=True)


def test_open_ended_and_backwards_ranges_are_refused():
    policy = PolicyEngine(PrivacyPolicySettings())
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(PolicyError, match="end must be after start"):
        policy.validate_range(start, start)
    with pytest.raises(PolicyError, match="must include a timezone"):
        policy.validate_range(datetime(2026, 1, 1), start + timedelta(days=1))


def test_result_limit_is_clamped_by_policy():
    policy = PolicyEngine(PrivacyPolicySettings(maximum_entry_result_limit=25))
    assert policy.clamp_limit(500, default=50, hard_max=500) == 25
    assert policy.clamp_limit(None, default=50, hard_max=500) == 25
    assert policy.clamp_limit(10, default=50, hard_max=500) == 10
    with pytest.raises(PolicyError):
        policy.clamp_limit(0, default=50, hard_max=500)


def test_lookback_days_beyond_the_window_requires_confirmation():
    policy = PolicyEngine(PrivacyPolicySettings())
    assert policy.clamp_lookback_days(30, hard_max=365, confirmed=False) == 30
    with pytest.raises(PolicyError, match="exceeds the 90-day limit"):
        policy.clamp_lookback_days(365, hard_max=365, confirmed=False)
    assert policy.clamp_lookback_days(365, hard_max=365, confirmed=True) == 365


def test_cross_domain_and_writeback_gates_are_closed():
    policy = PolicyEngine(PrivacyPolicySettings())
    with pytest.raises(PolicyError, match="separate explicit opt-in"):
        policy.require_cross_domain_correlation()
    with pytest.raises(PolicyError, match="disabled and unimplemented"):
        policy.require_writeback()


def test_network_guard_blocks_outbound_connections():
    """allow_external_network_calls: false is mechanically true, not a promise."""
    assert install_network_guard(PrivacyPolicySettings()) is True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="External network calls are disabled"):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_mcp_error_payloads_carry_the_policy_reason(ctx):
    payload = call_tool_sync(
        ctx,
        "mood.search_entries",
        {"start": START.isoformat(), "end": END.isoformat(), "include_notes": True},
    )
    assert payload["error"] == "policy_denied"
    assert "expose_raw_entries" in payload["message"]


def test_policy_snapshot_is_persisted_for_audit(ctx):
    conn = ctx.database.connect()
    try:
        rows = conn.execute("SELECT policy_json FROM consent_policies").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert json.loads(rows[0]["policy_json"])["expose_notes"] is False


def test_logs_never_contain_note_text(make_context, drop, caplog):
    ctx = make_context(privacy={"import_notes": True, "expose_notes": True})
    drop("notes.csv", csv_document(csv_row("n1", note=SECRET_NOTE)))
    with caplog.at_level(logging.DEBUG):
        ctx.imports.import_file(
            "notes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
        )
        ctx.summaries.summarize_period(start=START, end=END)
    assert SECRET_NOTE not in caplog.text


def test_note_safe_filter_drops_a_record_tagged_as_carrying_a_note(caplog):
    from lotus_mcp.database import logger as database_logger

    with caplog.at_level(logging.INFO):
        database_logger.info("leak %s", SECRET_NOTE, extra={"contains_note": True})
    assert SECRET_NOTE not in caplog.text
