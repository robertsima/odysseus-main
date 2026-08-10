"""Read-side tools: search, summaries, and low-energy observation."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from lotus_mcp.models import SourceType
from lotus_mcp.policy import PolicyError
from lotus_mcp.server import build_tools, call_tool_sync, dispatch, tool_name

START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 20, tzinfo=UTC)

HEADER = "id,timestamp,emotion,pleasantness,energy,intensity,tags\n"


def _rows(*specs: tuple[str, str, str, str, str, str]) -> str:
    body = "".join(
        f"{rid},{ts},{emotion},{valence},{energy},0.5,{tags}\n"
        for rid, ts, emotion, valence, energy, tags in specs
    )
    return HEADER + body


@pytest.fixture
def seeded(make_context, drop):
    """A context with raw-entry access enabled and a handful of entries."""
    ctx = make_context(privacy={"expose_raw_entries": True})
    drop(
        "seed.csv",
        _rows(
            # Mondays in August 2026: the 3rd, 10th, 17th.
            ("s1", "2026-08-03T08:00:00+00:00", "drained", "-0.5", "0.1", "work"),
            ("s2", "2026-08-03T09:00:00+00:00", "flat", "-0.4", "0.2", "work"),
            ("s3", "2026-08-10T08:30:00+00:00", "drained", "-0.6", "0.15", "work"),
            ("s4", "2026-08-10T14:00:00+00:00", "content", "0.5", "0.6", "lunch"),
            ("s5", "2026-08-12T09:00:00+00:00", "focused", "0.4", "0.8", "deep work"),
            ("s6", "2026-08-17T08:00:00+00:00", "drained", "-0.7", "0.05", "work"),
        ),
    )
    ctx.imports.import_file("seed.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    return ctx


# ---- search ---------------------------------------------------------------


def test_search_returns_entries_without_notes(seeded):
    result = seeded.queries.search_entries(start=START, end=END)
    assert result["count"] == 6
    assert result["notes_included"] is False
    assert all("note" not in entry for entry in result["entries"])


def test_search_filters_by_emotion_label(seeded):
    result = seeded.queries.search_entries(start=START, end=END, emotion_labels=["Drained"])
    assert result["count"] == 3
    assert {entry["emotion_label"] for entry in result["entries"]} == {"drained"}


def test_search_filters_by_tag(seeded):
    result = seeded.queries.search_entries(start=START, end=END, tags=["WORK"])
    assert result["count"] == 4


def test_search_paginates_with_an_opaque_cursor(seeded):
    first = seeded.queries.search_entries(start=START, end=END, limit=2)
    assert first["count"] == 2
    assert first["has_more"] is True

    second = seeded.queries.search_entries(
        start=START, end=END, limit=2, cursor=first["next_cursor"]
    )
    assert second["count"] == 2
    first_ids = {entry["id"] for entry in first["entries"]}
    assert not (first_ids & {entry["id"] for entry in second["entries"]})


def test_invalid_cursor_is_rejected(seeded):
    with pytest.raises(PolicyError, match="Invalid pagination cursor"):
        seeded.queries.search_entries(start=START, end=END, cursor="!!!not-base64!!!")


def test_limit_is_capped_by_policy(make_context, drop):
    ctx = make_context(privacy={"expose_raw_entries": True, "maximum_entry_result_limit": 2})
    drop(
        "seed.csv",
        _rows(
            ("s1", "2026-08-03T08:00:00+00:00", "a", "0.1", "0.1", ""),
            ("s2", "2026-08-04T08:00:00+00:00", "b", "0.1", "0.1", ""),
            ("s3", "2026-08-05T08:00:00+00:00", "c", "0.1", "0.1", ""),
        ),
    )
    ctx.imports.import_file("seed.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    result = ctx.queries.search_entries(start=START, end=END, limit=500)
    assert result["limit_applied"] == 2
    assert result["count"] == 2


@pytest.mark.parametrize(
    "hostile",
    [
        "drained' OR '1'='1",
        "'; DROP TABLE mood_entries; --",
        'drained") OR 1=1 --',
    ],
)
def test_filters_are_bound_parameters_not_sql(seeded, hostile: str):
    result = seeded.queries.search_entries(start=START, end=END, emotion_labels=[hostile])
    # Treated as a literal label that matches nothing; the table survives.
    assert result["count"] == 0
    assert seeded.database.counts()["entries"] == 6


def test_tag_filter_is_also_parameterised(seeded):
    result = seeded.queries.search_entries(
        start=START, end=END, tags=["work'); DELETE FROM entry_tags; --"]
    )
    assert result["count"] == 0
    assert seeded.database.counts()["entries"] == 6


def test_search_range_is_enforced(seeded):
    result = seeded.queries.search_entries(start=datetime(2026, 8, 11, tzinfo=UTC), end=END)
    assert result["count"] == 2


# ---- summaries -------------------------------------------------------------


def test_summary_works_without_raw_entry_access(ctx, drop):
    """The default policy denies raw entries but still allows aggregates."""
    drop(
        "seed.csv",
        _rows(("s1", "2026-08-03T08:00:00+00:00", "drained", "-0.5", "0.1", "work")),
    )
    ctx.imports.import_file("seed.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)

    with pytest.raises(PolicyError):
        ctx.queries.search_entries(start=START, end=END)

    result = ctx.summaries.summarize_period(start=START, end=END)
    assert result["total_entries"] == 1
    assert result["emotion_counts"] == {"drained": 1}
    assert result["notes_accessed"] is False


def test_summary_groups_and_averages(seeded):
    result = seeded.summaries.summarize_period(start=START, end=END, group_by="day")
    buckets = {bucket["bucket"]: bucket for bucket in result["buckets"]}
    assert buckets["2026-08-03"]["entry_count"] == 2
    assert buckets["2026-08-03"]["average_energy"] == pytest.approx(0.15)
    assert buckets["2026-08-03"]["emotion_counts"] == {"drained": 1, "flat": 1}


def test_summary_supports_every_group_by(seeded):
    for group_by in ("day", "week", "month", "hour_of_day", "day_of_week"):
        result = seeded.summaries.summarize_period(start=START, end=END, group_by=group_by)
        assert result["group_by"] == group_by
        assert result["buckets"]


def test_summary_rejects_an_unknown_group_by(seeded):
    with pytest.raises(ValueError, match="group_by must be one of"):
        seeded.summaries.summarize_period(start=START, end=END, group_by="fortnight")


def test_summary_reports_missing_dimensions(make_context, drop):
    ctx = make_context()
    drop("partial.csv", "timestamp,emotion\n2026-08-03T09:00:00+00:00,calm\n")
    ctx.imports.import_file("partial.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    result = ctx.summaries.summarize_period(start=START, end=END)
    assert result["missing_data"]["energy"] == 1
    assert any("recorded energy" in caution.lower() for caution in result["cautions"])
    assert result["buckets"][0]["average_energy"] is None


def test_summary_of_an_empty_range_says_so(ctx):
    result = ctx.summaries.summarize_period(start=START, end=END)
    assert result["total_entries"] == 0
    assert "No imported entries fall inside this range." in result["cautions"]


def test_summary_never_diagnoses(seeded):
    result = seeded.summaries.summarize_period(start=START, end=END)
    text = json.dumps(result).lower()
    assert "not a clinical" in result["disclaimer"].lower()
    for word in ("depress", "diagnos", "disorder", "symptom"):
        assert word not in text


# ---- low-energy observation ------------------------------------------------


def test_low_energy_reports_sample_size_and_stays_observational(seeded):
    result = seeded.summaries.detect_low_energy_patterns(
        lookback_days=30, group_by="day_of_week", now=datetime(2026, 8, 20, tzinfo=UTC)
    )
    assert result["sample_size"]["entries_in_window"] == 6
    assert result["sample_size"]["entries_with_energy_or_valence"] == 6
    assert result["sample_size"]["low_energy_entries"] == 4
    assert result["notes_accessed"] is False

    monday = next(b for b in result["buckets"] if b["bucket"] == "Monday")
    assert monday["low_energy_entries"] == 4
    assert any("low-energy check-ins on Monday" in text for text in result["observations"])

    text = json.dumps(result).lower()
    for word in ("depress", "diagnos", "disorder", "burnout"):
        assert word not in text


def test_low_energy_declines_to_conclude_from_sparse_data(seeded):
    result = seeded.summaries.detect_low_energy_patterns(
        lookback_days=30,
        minimum_entries=10,
        group_by="day_of_week",
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert result["observations"] == []
    assert any("minimum of 10" in text for text in result["limitations"])


def test_low_energy_handles_an_empty_window(ctx):
    result = ctx.summaries.detect_low_energy_patterns(now=datetime(2026, 8, 20, tzinfo=UTC))
    assert result["sample_size"]["entries_in_window"] == 0
    assert result["observations"] == []
    assert any("No imported entries" in text for text in result["limitations"])


def test_low_energy_excludes_entries_without_the_dimensions(make_context, drop):
    ctx = make_context()
    drop("no_values.csv", "timestamp,emotion\n2026-08-03T09:00:00+00:00,calm\n")
    ctx.imports.import_file("no_values.csv", source_type=SourceType.MANUAL_CSV, dry_run=False)
    result = ctx.summaries.detect_low_energy_patterns(now=datetime(2026, 8, 20, tzinfo=UTC))
    assert result["sample_size"]["entries_with_energy_or_valence"] == 0
    assert any("cannot be identified" in text for text in result["limitations"])


def test_low_energy_rejects_an_unsupported_group_by(seeded):
    with pytest.raises(ValueError, match="group_by must be one of"):
        seeded.summaries.detect_low_energy_patterns(group_by="month")


# ---- MCP surface -----------------------------------------------------------


def test_tool_names_follow_the_configured_style():
    assert tool_name("search_entries", "dotted") == "mood.search_entries"
    assert tool_name("search_entries", "underscore") == "mood_search_entries"
    assert next(tool.name for tool in build_tools("underscore")) == "mood_get_import_status"


def test_every_tool_declares_a_closed_input_schema():
    for tool in build_tools():
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.description


def test_dispatch_runs_a_summary_over_the_mcp_shape(seeded):
    payload = dispatch(
        seeded,
        "summarize_period",
        {"start": START.isoformat(), "end": END.isoformat(), "group_by": "week"},
    )
    assert payload["group_by"] == "week"
    assert payload["total_entries"] == 6


def test_unknown_tool_returns_a_safe_error(ctx):
    assert call_tool_sync(ctx, "mood.delete_everything", {})["error"] == "unknown_tool"


def test_bad_timestamp_argument_is_an_invalid_request(ctx):
    payload = call_tool_sync(ctx, "mood.summarize_period", {"start": "yesterday", "end": "today"})
    assert payload["error"] == "invalid_request"


def test_naive_timestamp_argument_is_refused(ctx):
    payload = call_tool_sync(
        ctx, "mood.summarize_period", {"start": "2026-08-01T00:00:00", "end": "2026-08-02T00:00:00"}
    )
    assert payload["error"] == "invalid_request"
    assert "UTC offset" in payload["message"]


def test_underscore_wire_names_are_accepted_too(seeded):
    payload = call_tool_sync(
        seeded, "mood_summarize_period", {"start": START.isoformat(), "end": END.isoformat()}
    )
    assert payload["total_entries"] == 6
