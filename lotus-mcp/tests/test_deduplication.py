"""Deduplication across files, within a file, and across overlapping exports."""

from __future__ import annotations

from lotus_mcp.models import MoodEntry, SourceType

from .conftest import csv_document, csv_row

NO_ID_HEADER = "timestamp,emotion,pleasantness,energy,intensity,tags\n"


def _no_id_document(*rows: str) -> str:
    return NO_ID_HEADER + "".join(rows)


def _no_id_row(timestamp: str = "2026-08-03T09:15:00-04:00", emotion: str = "calm") -> str:
    return f"{timestamp},{emotion},0.1,0.5,0.5,work\n"


def test_identical_file_is_recognised_by_hash(ctx, copy_fixture):
    copy_fixture("sample_moods.csv")
    first = ctx.imports.import_file(
        "sample_moods.csv", source_type=SourceType.MANUAL_CSV, dry_run=False
    )
    assert first.inserted_count == 5

    copy_fixture("sample_moods.csv")
    second = ctx.imports.import_file(
        "sample_moods.csv", source_type=SourceType.MANUAL_CSV, dry_run=False
    )
    assert second.status == "duplicate_file"
    assert second.inserted_count == 0
    # Reported as 5 duplicates, not a bare 0/0 that reads like an empty file.
    assert second.duplicate_count == 5
    assert ctx.database.counts()["entries"] == 5


def test_overlapping_exports_deduplicate_by_source_record_id(ctx, drop):
    drop(
        "first.csv",
        csv_document(csv_row("x1"), csv_row("x2", timestamp="2026-08-04T09:00:00+00:00")),
    )
    ctx.imports.import_file("first.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    # A later export repeats x2 (with an edited label) and adds x3.
    drop(
        "second.csv",
        csv_document(
            csv_row("x2", timestamp="2026-08-04T09:00:00+00:00", emotion="edited-label"),
            csv_row("x3", timestamp="2026-08-05T09:00:00+00:00"),
        ),
    )
    report = ctx.imports.import_file("second.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    # The id wins over the changed label: x2 is a duplicate, only x3 is new.
    assert report.inserted_count == 1
    assert report.duplicate_count == 1
    assert ctx.database.counts()["entries"] == 3


def test_records_without_ids_deduplicate_by_fingerprint(ctx, drop):
    drop("a.csv", _no_id_document(_no_id_row()))
    ctx.imports.import_file("a.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    # Same content, different file name and hash.
    drop("b.csv", _no_id_document(_no_id_row(), _no_id_row(timestamp="2026-08-09T09:15:00-04:00")))
    report = ctx.imports.import_file("b.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    assert report.duplicate_count == 1
    assert report.inserted_count == 1
    assert ctx.database.counts()["entries"] == 2


def test_duplicates_within_one_file_collapse(ctx, drop):
    drop("dupes.csv", _no_id_document(_no_id_row(), _no_id_row(), _no_id_row()))
    report = ctx.imports.import_file("dupes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    assert report.record_count == 3
    assert report.inserted_count == 1
    assert report.duplicate_count == 2


def test_dry_run_previews_duplicate_counts_without_writing(ctx, drop):
    drop("a.csv", _no_id_document(_no_id_row()))
    ctx.imports.import_file("a.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    drop("b.csv", _no_id_document(_no_id_row(), _no_id_row(timestamp="2026-08-09T09:15:00-04:00")))
    preview = ctx.imports.import_file("b.csv", source_type=SourceType.MANUAL_CSV)
    assert preview.duplicate_count == 1
    assert preview.inserted_count == 1
    assert ctx.database.counts()["entries"] == 1  # unchanged


def test_fingerprint_excludes_raw_note_text():
    """The note contributes as a hash, so the digest is safe to log or return."""
    common = {
        "source": "manual_csv",
        "occurred_at": "2026-08-03T09:15:00-04:00",
        "emotion_label": "tired",
    }
    with_note = MoodEntry(**common, note="private journal text")
    without_note = MoodEntry(**common)
    same_note = MoodEntry(**common, note="private journal text")

    assert with_note.fingerprint() != without_note.fingerprint()
    assert with_note.fingerprint() == same_note.fingerprint()
    assert "private" not in with_note.fingerprint()


def test_fingerprint_is_stable_across_tag_order():
    common = {"source": "manual_csv", "occurred_at": "2026-08-03T09:15:00-04:00"}
    assert (
        MoodEntry(**common, tags=["work", "planning"]).fingerprint()
        == MoodEntry(**common, tags=["planning", "work"]).fingerprint()
    )


def test_fingerprint_matches_across_equivalent_offsets():
    """The same instant written in two zones is one entry, not two."""
    utc = MoodEntry(source="manual_csv", occurred_at="2026-08-03T13:15:00+00:00")
    eastern = MoodEntry(source="manual_csv", occurred_at="2026-08-03T09:15:00-04:00")
    assert utc.fingerprint() == eastern.fingerprint()


def test_rollback_removes_only_its_own_batch(ctx, drop):
    drop("a.csv", csv_document(csv_row("a1")))
    first = ctx.imports.import_file("a.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    drop("b.csv", csv_document(csv_row("b1", timestamp="2026-08-06T09:00:00+00:00")))
    ctx.imports.import_file("b.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    assert ctx.database.counts()["entries"] == 2

    deleted = ctx.database.rollback_batch(first.batch_id)
    assert deleted == 1
    assert ctx.database.counts()["entries"] == 1

    statuses = {b["batch_id"]: b["status"] for b in ctx.imports.get_import_status()}
    assert statuses[first.batch_id] == "rolled_back"


def test_redacting_notes_keeps_deduplication_working(make_context, drop):
    ctx = make_context(privacy={"import_notes": True, "expose_notes": True})
    drop("notes.csv", csv_document(csv_row("n1", note="synthetic note")))
    ctx.imports.import_file(
        "notes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )
    assert ctx.database.redact_all_notes() == 1
    assert ctx.database.counts()["entries_with_notes"] == 0

    # A later export repeats that entry and adds one more. The repeat is still
    # recognised, because the note hash survived redaction — and the file hash
    # differs, so this exercises the record-level path rather than the
    # duplicate-file shortcut.
    drop(
        "notes_again.csv",
        csv_document(
            csv_row("n1", note="synthetic note"),
            csv_row("n2", timestamp="2026-08-06T09:00:00+00:00", note="another note"),
        ),
    )
    report = ctx.imports.import_file(
        "notes_again.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )
    assert report.status == "success"
    assert report.inserted_count == 1
    assert report.duplicate_count == 1
