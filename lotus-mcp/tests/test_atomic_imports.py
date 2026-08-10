"""Atomicity, file disposition, and what import status is allowed to reveal."""

from __future__ import annotations

import json
import sqlite3

import pytest

from lotus_mcp.database import Database
from lotus_mcp.models import SourceType

from .conftest import csv_document, csv_row

SECRET_NOTE = "SYNTHETIC-JOURNAL-MARKER"


def test_dry_run_mutates_nothing(ctx, drop, incoming, workspace):
    drop(
        "clean.csv",
        csv_document(csv_row("a1"), csv_row("a2", timestamp="2026-08-04T09:00:00+00:00")),
    )
    report = ctx.imports.import_file("clean.csv", source_type=SourceType.MANUAL_CSV, dry_run=True)

    assert report.status == "dry_run"
    assert report.file_disposition == "unchanged"
    assert ctx.database.counts() == {"entries": 0, "batches": 0, "entries_with_notes": 0}
    assert (incoming / "clean.csv").exists()
    assert not list((workspace / "imports/processed").iterdir())
    assert not list((workspace / "imports/failed").iterdir())


def test_dry_run_of_a_broken_file_does_not_move_it(ctx, drop, incoming, workspace):
    drop("broken.csv", "timestamp,note,note\n2026-08-03T09:00:00+00:00,a,b\n")
    report = ctx.imports.import_file("broken.csv", source_type=SourceType.MANUAL_CSV, dry_run=True)

    assert report.status == "dry_run"
    assert "Duplicate column" in report.message
    # Failure handling belongs to a real import; a dry run only reports.
    assert (incoming / "broken.csv").exists()
    assert not list((workspace / "imports/failed").iterdir())
    assert ctx.database.counts()["batches"] == 0


def test_successful_import_moves_the_file_to_processed(ctx, drop, incoming, workspace):
    drop("good.csv", csv_document(csv_row("a1")))
    report = ctx.imports.import_file("good.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    assert report.file_disposition == "moved_to_processed"
    assert not (incoming / "good.csv").exists()
    assert (workspace / "imports/processed/good.csv").exists()


def test_failed_import_moves_the_file_to_failed(ctx, drop, incoming, workspace):
    drop("broken.csv", "timestamp,note,note\n2026-08-03T09:00:00+00:00,a,b\n")
    report = ctx.imports.import_file("broken.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    assert report.status == "validation_failed"
    assert report.file_disposition == "moved_to_failed"
    assert not (incoming / "broken.csv").exists()
    # Recoverable: the bytes are untouched, just relocated.
    assert (workspace / "imports/failed/broken.csv").exists()


def test_a_repeated_filename_never_overwrites_an_earlier_file(ctx, drop, workspace):
    drop("same.csv", csv_document(csv_row("a1")))
    ctx.imports.import_file("same.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    drop("same.csv", csv_document(csv_row("b1", timestamp="2026-08-06T09:00:00+00:00")))
    ctx.imports.import_file("same.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    processed = sorted(p.name for p in (workspace / "imports/processed").iterdir())
    # The first file keeps its name; the second is timestamp-suffixed rather
    # than clobbering it.
    assert len(processed) == 2
    assert "same.csv" in processed
    assert any(name != "same.csv" and name.startswith("same.") for name in processed)


def test_commit_failure_rolls_back_the_whole_batch(ctx, drop, monkeypatch, incoming):
    """A failure part-way through leaves no entries and no success row."""
    drop(
        "many.csv",
        csv_document(
            csv_row("a1"),
            csv_row("a2", timestamp="2026-08-04T09:00:00+00:00"),
            csv_row("a3", timestamp="2026-08-05T09:00:00+00:00"),
        ),
    )

    calls = {"n": 0}
    original = Database.insert_entry

    def failing_insert(conn, entry, batch_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return original(conn, entry, batch_id)

    monkeypatch.setattr(Database, "insert_entry", staticmethod(failing_insert))
    report = ctx.imports.import_file("many.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    assert report.status == "commit_failed"
    assert report.inserted_count == 0
    # Nothing partial survived, and the batch row that references entries is gone.
    assert ctx.database.counts()["entries"] == 0
    statuses = [b["status"] for b in ctx.imports.get_import_status()]
    assert statuses == ["commit_failed"]


def test_commit_failure_leaves_the_source_file_recoverable(
    ctx, drop, monkeypatch, incoming, workspace
):
    drop("many.csv", csv_document(csv_row("a1")))

    def failing_insert(conn, entry, batch_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "insert_entry", staticmethod(failing_insert))
    report = ctx.imports.import_file("many.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    assert report.file_disposition == "left_in_incoming"
    assert (incoming / "many.csv").exists()
    assert not list((workspace / "imports/failed").iterdir())


def test_commit_failure_message_carries_no_database_internals(ctx, drop, monkeypatch):
    drop("many.csv", csv_document(csv_row("a1")))

    def failing_insert(conn, entry, batch_id):
        raise sqlite3.OperationalError("no such column: super_secret_internal")

    monkeypatch.setattr(Database, "insert_entry", staticmethod(failing_insert))
    report = ctx.imports.import_file("many.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    assert "super_secret_internal" not in report.model_dump_json()


def test_a_move_failure_does_not_lose_the_committed_data(ctx, drop, monkeypatch, incoming):
    """Database first, file second: a failed move never costs the import."""
    drop("good.csv", csv_document(csv_row("a1")))
    monkeypatch.setattr(
        "lotus_mcp.services.import_service.os.replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")),
    )
    monkeypatch.setattr(
        "lotus_mcp.services.import_service.shutil.move",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")),
    )
    report = ctx.imports.import_file("good.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    assert report.status == "success"
    assert report.file_disposition == "left_in_incoming"
    assert ctx.database.counts()["entries"] == 1
    assert (incoming / "good.csv").exists()

    # And re-importing the stranded file is recognised by hash, not duplicated.
    again = ctx.imports.import_file("good.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    assert again.status == "duplicate_file"
    assert ctx.database.counts()["entries"] == 1


# ---- import status ---------------------------------------------------------


def test_import_status_exposes_no_notes_or_host_paths(make_context, drop):
    ctx = make_context(privacy={"import_notes": True, "expose_notes": True})
    drop("notes.csv", csv_document(csv_row("n1", note=SECRET_NOTE)))
    ctx.imports.import_file(
        "notes.csv", source_type=SourceType.MANUAL_CSV, dry_run=False, include_notes=True
    )

    payload = json.dumps(ctx.imports.get_import_status())
    assert SECRET_NOTE not in payload
    assert str(ctx.config.paths.import_root) not in payload
    assert str(ctx.config.paths.processed_dir) not in payload

    batch = ctx.imports.get_import_status()[0]
    assert batch["filename"] == "notes.csv"  # basename only
    assert batch["status"] == "success"
    assert batch["notes_imported"] is True
    assert set(batch) == {
        "batch_id",
        "source",
        "source_type",
        "filename",
        "file_hash_prefix",
        "imported_at",
        "record_count",
        "inserted_count",
        "duplicate_count",
        "rejected_count",
        "status",
        "message",
        "parser_version",
        "mapping_name",
        "notes_imported",
    }


def test_import_status_distinguishes_every_outcome(ctx, drop):
    drop("ok.csv", csv_document(csv_row("a1")))
    ctx.imports.import_file("ok.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    drop("ok.csv", csv_document(csv_row("a1")))
    ctx.imports.import_file("ok.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    drop("bad.csv", "timestamp,note,note\n2026-08-03T09:00:00+00:00,a,b\n")
    ctx.imports.import_file("bad.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    statuses = {b["status"] for b in ctx.imports.get_import_status()}
    assert statuses == {"success", "duplicate_file", "validation_failed"}


def test_import_status_limit_is_bounded(ctx, drop):
    for index in range(3):
        drop(
            f"f{index}.csv",
            csv_document(csv_row(f"a{index}", timestamp=f"2026-08-0{index + 1}T09:00:00+00:00")),
        )
        ctx.imports.import_file(f"f{index}.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    assert len(ctx.imports.get_import_status(limit=2)) == 2
    assert len(ctx.imports.get_import_status(limit=10_000)) == 3


def test_report_never_echoes_raw_row_content(make_context, drop):
    ctx = make_context(privacy={"import_notes": True, "expose_notes": True})
    drop(
        "mixed.csv",
        csv_document(
            csv_row("n1", note=SECRET_NOTE),
            csv_row("n2", timestamp="bad-timestamp", note=SECRET_NOTE),
        ),
    )
    report = ctx.imports.import_file(
        "mixed.csv", source_type=SourceType.MANUAL_CSV, include_notes=True
    )
    assert report.rejected_count == 1
    assert SECRET_NOTE not in report.model_dump_json()


@pytest.mark.parametrize("status_field", ["record_count", "inserted_count", "duplicate_count"])
def test_reported_counts_are_integers(ctx, copy_fixture, status_field):
    copy_fixture("sample_moods.csv")
    report = ctx.imports.import_file(
        "sample_moods.csv", source_type=SourceType.MANUAL_CSV, dry_run=False
    )
    assert isinstance(getattr(report, status_field), int)
