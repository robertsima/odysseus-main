"""JSON parsing and its structural defences."""

from __future__ import annotations

import json

from lotus_mcp.importers.json_importer import scan_max_depth
from lotus_mcp.models import SourceType


def test_valid_json_import(ctx, copy_fixture):
    copy_fixture("sample_moods.json")
    preview = ctx.imports.import_file("sample_moods.json", source_type=SourceType.MANUAL_JSON)
    assert preview.status == "dry_run"
    assert preview.record_count == 3

    committed = ctx.imports.import_file(
        "sample_moods.json", source_type=SourceType.MANUAL_JSON, dry_run=False
    )
    assert committed.status == "success"
    assert committed.inserted_count == 3


def test_top_level_list_is_accepted(ctx, drop):
    drop(
        "list.json",
        json.dumps([{"timestamp": "2026-08-03T09:00:00+00:00", "emotion": "calm"}]),
    )
    report = ctx.imports.import_file("list.json", source_type=SourceType.MANUAL_JSON)
    assert report.record_count == 1


def test_malformed_json_reports_position_not_content(ctx, drop):
    drop("broken.json", '[{"timestamp": "2026-08-03T09:00:00+00:00", "note": "secret text"')
    report = ctx.imports.import_file("broken.json", source_type=SourceType.MANUAL_JSON)
    assert "not valid JSON" in report.message
    assert "secret text" not in report.message


def test_deeply_nested_json_is_refused(make_context, drop):
    ctx = make_context(limits={"max_json_depth": 5})
    payload = {"entries": [{"timestamp": "2026-08-03T09:00:00+00:00"}]}
    nested: object = "leaf"
    for _ in range(10):
        nested = {"deeper": nested}
    payload["entries"][0]["extra"] = nested
    drop("deep.json", json.dumps(payload))
    report = ctx.imports.import_file("deep.json", source_type=SourceType.MANUAL_JSON)
    assert "nesting exceeds" in report.message


def test_scan_max_depth_ignores_brackets_inside_strings():
    assert scan_max_depth('{"a": "[[[[["}') == 1
    assert scan_max_depth('{"a": {"b": [1]}}') == 3
    assert scan_max_depth(r'{"a": "\"[["}') == 1


def test_non_object_record_is_refused(ctx, drop):
    drop("scalars.json", json.dumps(["not-an-object"]))
    report = ctx.imports.import_file("scalars.json", source_type=SourceType.MANUAL_JSON)
    assert "not a JSON object" in report.message


def test_object_without_a_record_list_is_refused(ctx, drop):
    drop("wrong_shape.json", json.dumps({"mood": "calm"}))
    report = ctx.imports.import_file("wrong_shape.json", source_type=SourceType.MANUAL_JSON)
    assert "must contain a list" in report.message


def test_tag_array_becomes_tags(make_context, drop):
    ctx = make_context(privacy={"expose_raw_entries": True})
    drop(
        "tags.json",
        json.dumps(
            [
                {
                    "timestamp": "2026-08-03T09:00:00+00:00",
                    "emotion": "calm",
                    "tags": ["work", "planning", "work"],
                }
            ]
        ),
    )
    ctx.imports.import_file("tags.json", source_type=SourceType.MANUAL_JSON, dry_run=False)
    conn = ctx.database.connect()
    try:
        stored = json.loads(conn.execute("SELECT tags FROM mood_entries").fetchone()[0])
        tag_rows = conn.execute("SELECT tag FROM entry_tags ORDER BY tag").fetchall()
    finally:
        conn.close()
    # Duplicates collapse; a tag index row exists for each distinct tag.
    assert stored == ["work", "planning"]
    assert [row["tag"] for row in tag_rows] == ["planning", "work"]


def test_nested_object_value_is_flattened_to_text(make_context, drop):
    ctx = make_context(
        privacy={"expose_raw_entries": True},
        mappings={
            "keep": {
                "source_label": "manual_json",
                "aliases": {"occurred_at": ["timestamp"]},
                "retain_unknown_fields": True,
            }
        },
    )
    drop(
        "nested.json",
        json.dumps([{"timestamp": "2026-08-03T09:00:00+00:00", "place": {"city": "Ottawa"}}]),
    )
    report = ctx.imports.import_file(
        "nested.json", source_type=SourceType.MANUAL_JSON, mapping_name="keep", dry_run=False
    )
    assert report.inserted_count == 1
    conn = ctx.database.connect()
    try:
        context = json.loads(conn.execute("SELECT context FROM mood_entries").fetchone()[0])
    finally:
        conn.close()
    # Stored inert, as a bounded string — not as a live nested structure.
    assert isinstance(context["place"], str)
    assert "Ottawa" in context["place"]


def test_empty_json_file_is_refused(ctx, drop):
    drop("empty.json", "   ")
    report = ctx.imports.import_file("empty.json", source_type=SourceType.MANUAL_JSON)
    assert "empty" in report.message.lower()


def test_too_many_records_is_refused(make_context, drop):
    ctx = make_context(limits={"max_records_per_file": 2})
    drop(
        "many.json",
        json.dumps(
            [{"timestamp": f"2026-08-0{i}T09:00:00+00:00", "emotion": "calm"} for i in range(1, 5)]
        ),
    )
    report = ctx.imports.import_file("many.json", source_type=SourceType.MANUAL_JSON)
    assert "more than 2 records" in report.message
