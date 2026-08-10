"""CSV parsing, normalization, and per-record validation."""

from __future__ import annotations

import pytest

from lotus_mcp.models import SourceType
from lotus_mcp.services.import_service import ImportRefused

from .conftest import csv_document, csv_row

NOTE_POLICY = {"import_notes": True, "expose_notes": True, "expose_raw_entries": True}


def test_valid_csv_dry_run_then_commit(ctx, copy_fixture):
    copy_fixture("sample_moods.csv")

    preview = ctx.imports.import_file("sample_moods.csv", source_type=SourceType.MANUAL_CSV)
    assert preview.status == "dry_run"
    assert preview.record_count == 5
    assert preview.inserted_count == 5
    assert preview.rejected_count == 0

    committed = ctx.imports.import_file(
        "sample_moods.csv", source_type=SourceType.MANUAL_CSV, dry_run=False
    )
    assert committed.status == "success"
    assert committed.inserted_count == 5
    assert ctx.database.counts()["entries"] == 5


def test_resolved_columns_are_reported(ctx, copy_fixture):
    copy_fixture("sample_moods.csv")
    report = ctx.imports.import_file("sample_moods.csv", source_type=SourceType.MANUAL_CSV)
    # The mapping must be inspectable: the caller can see which column fed
    # which field rather than trusting an opaque guess.
    assert report.resolved_columns["occurred_at"] == "timestamp"
    assert report.resolved_columns["emotion_label"] == "emotion"
    assert report.resolved_columns["valence"] == "pleasantness"


def test_missing_timestamp_column_is_rejected(ctx, drop):
    drop("no_time.csv", "emotion,energy\nhappy,0.5\n")
    report = ctx.imports.import_file("no_time.csv", source_type=SourceType.MANUAL_CSV)
    assert "No timestamp column" in report.message


def test_missing_timestamp_value_rejects_only_that_row(ctx, drop):
    drop("mixed.csv", csv_document(csv_row("a"), csv_row("b", timestamp="")))
    report = ctx.imports.import_file("mixed.csv", source_type=SourceType.MANUAL_CSV)
    assert report.record_count == 2
    assert report.rejected_count == 1
    assert report.problems[0].field == "occurred_at"
    assert report.problems[0].detail == "missing timestamp"


def test_invalid_timestamp_is_rejected(ctx, drop):
    drop("bad_time.csv", csv_document(csv_row(timestamp="not-a-date")))
    report = ctx.imports.import_file("bad_time.csv", source_type=SourceType.MANUAL_CSV)
    assert report.rejected_count == 1
    assert "timestamp" in report.problems[0].detail


def test_timezone_aware_timestamp_is_preserved(make_context, drop):
    ctx = make_context(privacy={"expose_raw_entries": True})
    drop("tz.csv", csv_document(csv_row(timestamp="2026-08-03T09:15:00-04:00")))
    ctx.imports.import_file("tz.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    conn = ctx.database.connect()
    try:
        row = conn.execute(
            "SELECT occurred_at_utc, utc_offset_minutes FROM mood_entries"
        ).fetchone()
    finally:
        conn.close()
    # Stored as UTC, with the original offset kept so local buckets stay right.
    assert row["occurred_at_utc"] == "2026-08-03T13:15:00+00:00"
    assert row["utc_offset_minutes"] == -240


def test_naive_timestamp_is_rejected_not_guessed(ctx, drop):
    drop("naive.csv", csv_document(csv_row(timestamp="2026-08-03 09:15:00")))
    report = ctx.imports.import_file("naive.csv", source_type=SourceType.MANUAL_CSV)
    assert report.rejected_count == 1
    assert "no timezone" in report.problems[0].detail


def test_declared_timezone_resolves_naive_timestamps(make_context, drop):
    ctx = make_context(
        mappings={
            "declared": {
                "source_label": "manual_csv",
                "assume_timezone": "America/New_York",
                "aliases": {"occurred_at": ["timestamp"], "emotion_label": ["emotion"]},
            }
        }
    )
    drop("naive.csv", csv_document(csv_row(timestamp="2026-08-03 09:15:00")))
    report = ctx.imports.import_file(
        "naive.csv", source_type=SourceType.MANUAL_CSV, mapping_name="declared"
    )
    assert report.rejected_count == 0
    assert report.inserted_count == 1


def test_daylight_saving_ambiguous_local_time_is_rejected(make_context, drop):
    """01:30 on the US fall-back date exists twice; neither answer is correct."""
    ctx = make_context(
        mappings={
            "declared": {
                "source_label": "manual_csv",
                "assume_timezone": "America/New_York",
                "aliases": {"occurred_at": ["timestamp"], "emotion_label": ["emotion"]},
            }
        }
    )
    drop("dst.csv", csv_document(csv_row(timestamp="2026-11-01 01:30:00")))
    report = ctx.imports.import_file(
        "dst.csv", source_type=SourceType.MANUAL_CSV, mapping_name="declared"
    )
    assert report.rejected_count == 1
    assert "ambiguous" in report.problems[0].detail


def test_split_date_and_time_columns(make_context, drop):
    ctx = make_context(
        mappings={
            "split": {
                "source_label": "manual_csv",
                "assume_timezone": "UTC",
                "aliases": {"date": ["date"], "time": ["time"], "emotion_label": ["mood"]},
            }
        }
    )
    drop("split.csv", "date,time,mood\n2026-08-03,09:15,calm\n")
    report = ctx.imports.import_file(
        "split.csv", source_type=SourceType.MANUAL_CSV, mapping_name="split"
    )
    assert report.inserted_count == 1


def test_date_without_time_is_rejected(make_context, drop):
    ctx = make_context(
        mappings={
            "split": {
                "source_label": "manual_csv",
                "assume_timezone": "UTC",
                "aliases": {"date": ["date"], "time": ["time"], "emotion_label": ["mood"]},
            }
        }
    )
    drop("split.csv", "date,time,mood\n2026-08-03,,calm\n")
    report = ctx.imports.import_file(
        "split.csv", source_type=SourceType.MANUAL_CSV, mapping_name="split"
    )
    assert report.problems[0].detail == "date supplied without a time"


def test_scales_rescale_source_values(make_context, drop):
    ctx = make_context(
        privacy={"expose_raw_entries": True},
        mappings={
            "five": {
                "source_label": "manual_csv",
                "aliases": {"occurred_at": ["timestamp"], "valence": ["pleasantness"]},
                "scales": {"valence": {"in_min": 1, "in_max": 5}},
            }
        },
    )
    drop("scaled.csv", "timestamp,pleasantness\n2026-08-03T09:00:00+00:00,5\n")
    ctx.imports.import_file(
        "scaled.csv", source_type=SourceType.MANUAL_CSV, mapping_name="five", dry_run=False
    )
    conn = ctx.database.connect()
    try:
        assert conn.execute("SELECT valence FROM mood_entries").fetchone()["valence"] == 1.0
    finally:
        conn.close()


def test_out_of_range_value_rejects_the_row(ctx, drop):
    drop("range.csv", csv_document(csv_row(valence="9")))
    report = ctx.imports.import_file("range.csv", source_type=SourceType.MANUAL_CSV)
    assert report.rejected_count == 1


def test_non_numeric_value_rejects_the_row(ctx, drop):
    drop("nan.csv", csv_document(csv_row(energy="high")))
    report = ctx.imports.import_file("nan.csv", source_type=SourceType.MANUAL_CSV)
    assert report.problems[0].detail == "not a number"


def test_unicode_and_emoji_labels_survive(make_context, drop):
    ctx = make_context(privacy={"expose_raw_entries": True})
    drop("unicode.csv", csv_document(csv_row(emotion="übermüdet 😴")))
    ctx.imports.import_file("unicode.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    conn = ctx.database.connect()
    try:
        assert (
            conn.execute("SELECT emotion_label FROM mood_entries").fetchone()[0] == "übermüdet 😴"
        )
    finally:
        conn.close()


def test_multiline_quoted_note_is_imported_intact(make_context, drop):
    ctx = make_context(privacy=NOTE_POLICY)
    drop(
        "multiline.csv",
        'id,timestamp,emotion,note\nr1,2026-08-03T09:00:00+00:00,tired,"line one\nline two"\n',
    )
    report = ctx.imports.import_file(
        "multiline.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )
    assert report.inserted_count == 1
    conn = ctx.database.connect()
    try:
        assert conn.execute("SELECT note FROM mood_entries").fetchone()[0] == "line one\nline two"
    finally:
        conn.close()


def test_empty_note_stores_null(make_context, drop):
    ctx = make_context(privacy=NOTE_POLICY)
    drop("empty_note.csv", csv_document(csv_row(note="")))
    ctx.imports.import_file(
        "empty_note.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )
    conn = ctx.database.connect()
    try:
        assert conn.execute("SELECT note FROM mood_entries").fetchone()[0] is None
    finally:
        conn.close()


def test_oversized_note_is_rejected_without_echoing_it(make_context, drop):
    ctx = make_context(privacy=NOTE_POLICY, limits={"max_note_chars": 20})
    secret = "S3CRET-JOURNAL-TEXT" * 5
    drop("big_note.csv", csv_document(csv_row(note=secret)))
    report = ctx.imports.import_file(
        "big_note.csv", source_type=SourceType.MANUAL_CSV, include_notes=True
    )
    assert report.rejected_count == 1
    assert "max_note_chars" in report.problems[0].detail
    assert "S3CRET" not in report.model_dump_json()


def test_oversized_note_truncates_when_configured(make_context, drop):
    ctx = make_context(
        privacy=NOTE_POLICY, limits={"max_note_chars": 10, "oversize_note_policy": "truncate"}
    )
    drop("trunc.csv", csv_document(csv_row(note="abcdefghijklmnop")))
    ctx.imports.import_file(
        "trunc.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )
    conn = ctx.database.connect()
    try:
        assert conn.execute("SELECT note FROM mood_entries").fetchone()[0] == "abcdefghij"
    finally:
        conn.close()


def test_duplicate_column_names_are_refused(ctx, drop):
    drop("dupes.csv", "timestamp,note,note\n2026-08-03T09:00:00+00:00,a,b\n")
    report = ctx.imports.import_file("dupes.csv", source_type=SourceType.MANUAL_CSV)
    assert "Duplicate column" in report.message


def test_row_with_more_fields_than_header_is_refused(ctx, drop):
    drop("ragged.csv", "timestamp,emotion\n2026-08-03T09:00:00+00:00,calm,extra\n")
    report = ctx.imports.import_file("ragged.csv", source_type=SourceType.MANUAL_CSV)
    assert "more fields than the header" in report.message


def test_non_utf8_file_is_refused(ctx, incoming):
    (incoming / "latin1.csv").write_bytes(b"timestamp,emotion\n2026-08-03T09:00:00+00:00,caf\xe9\n")
    report = ctx.imports.import_file("latin1.csv", source_type=SourceType.MANUAL_CSV)
    assert "not valid UTF-8" in report.message


def test_header_only_file_is_refused(ctx, drop):
    drop("headers.csv", "timestamp,emotion\n")
    report = ctx.imports.import_file("headers.csv", source_type=SourceType.MANUAL_CSV)
    assert "no data rows" in report.message


def test_formula_cell_is_stored_as_inert_text(make_context, drop):
    """A cell starting with '=' is data here; nothing evaluates it."""
    ctx = make_context(privacy={"expose_raw_entries": True})
    drop("formula.csv", csv_document(csv_row(emotion='=HYPERLINK("http://x")')))
    ctx.imports.import_file("formula.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    conn = ctx.database.connect()
    try:
        stored = conn.execute("SELECT emotion_label FROM mood_entries").fetchone()[0]
    finally:
        conn.close()
    assert stored.startswith("=HYPERLINK")


def test_oversized_file_is_refused_before_parsing(make_context, drop):
    ctx = make_context(limits={"max_file_bytes": 1024})
    drop("big.csv", csv_document(*[csv_row(f"r{i}") for i in range(200)]))
    with pytest.raises(ImportRefused, match="larger than"):
        ctx.imports.import_file("big.csv", source_type=SourceType.MANUAL_CSV)


def test_unsupported_extension_is_refused(ctx, drop):
    drop("notes.txt", "anything")
    with pytest.raises(ImportRefused, match="Unsupported file type"):
        ctx.imports.import_file("notes.txt", source_type=SourceType.MANUAL_CSV)


def test_unparsed_source_types_are_refused_not_guessed(ctx, copy_fixture):
    copy_fixture("sample_moods.csv")
    with pytest.raises(ImportRefused, match="No verified parser"):
        ctx.imports.import_file("sample_moods.csv", source_type=SourceType.APPLE_HEALTH_EXPORT)


def test_unknown_mapping_name_is_refused(ctx, copy_fixture):
    copy_fixture("sample_moods.csv")
    with pytest.raises(ImportRefused, match="Unknown mapping"):
        ctx.imports.import_file(
            "sample_moods.csv", source_type=SourceType.MANUAL_CSV, mapping_name="does_not_exist"
        )


def test_retain_unknown_fields_is_opt_in(make_context, drop):
    ctx = make_context(
        privacy={"expose_raw_entries": True},
        mappings={
            "keep": {
                "source_label": "manual_csv",
                "aliases": {"occurred_at": ["timestamp"]},
                "retain_unknown_fields": True,
            }
        },
    )
    drop("extra.csv", "timestamp,weather\n2026-08-03T09:00:00+00:00,rain\n")
    ctx.imports.import_file(
        "extra.csv", source_type=SourceType.MANUAL_CSV, mapping_name="keep", dry_run=False
    )
    conn = ctx.database.connect()
    try:
        assert '"weather": "rain"' in conn.execute("SELECT context FROM mood_entries").fetchone()[0]
    finally:
        conn.close()
