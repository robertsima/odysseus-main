"""Per-endpoint/model context tuning.

The same knob wants opposite values on a 400k hosted model and an 8k local one,
so the profile is resolved per (endpoint, model) and the resolution has to be
explicit about which source won — a setting that silently does nothing is worse
than no setting.
"""
import pytest

from src import context_profiles as cp


CODEX = "https://chatgpt.com/backend-api/codex/responses"
LOCAL = "http://localhost:11434/v1"


@pytest.fixture()
def stored(monkeypatch):
    """Swap the settings-backed store for an in-memory one."""
    data = {}
    monkeypatch.setattr(cp, "_stored_profiles", lambda: data)
    return data


def test_recommended_preset_follows_the_window():
    assert cp.preset_for_window(400_000) == "long_context"
    assert cp.preset_for_window(128_000) == "balanced"
    assert cp.preset_for_window(8_192) == "compact"
    assert cp.preset_for_window(0) == "balanced", "unknown window must not guess big"


def test_a_long_context_model_keeps_more_inline_than_a_small_one(stored):
    big = cp.resolve(CODEX, "gpt-5.4", 400_000)
    small = cp.resolve(LOCAL, "qwen3:8b", 8_192)
    assert big["tool_output_inline_limit"] > small["tool_output_inline_limit"]
    assert big["trim_target_ratio"] >= small["trim_target_ratio"]


def test_model_profile_beats_endpoint_beats_global(stored):
    stored[cp.GLOBAL_KEY] = {"preset": "compact"}
    assert cp.resolve(CODEX, "gpt-5.4", 400_000)["_source"] == "global"

    stored[cp.profile_key(CODEX, "")] = {"preset": "balanced"}
    assert cp.resolve(CODEX, "gpt-5.4", 400_000)["_source"] == "endpoint"

    stored[cp.profile_key(CODEX, "gpt-5.4")] = {"preset": "long_context"}
    resolved = cp.resolve(CODEX, "gpt-5.4", 400_000)
    assert resolved["_source"] == "model"
    assert resolved["tool_output_inline_limit"] == 8000

    # A different model on the same endpoint still falls back to the endpoint.
    assert cp.resolve(CODEX, "gpt-5-mini", 400_000)["_source"] == "endpoint"


def test_a_saved_profile_overrides_the_environment(stored, monkeypatch):
    """Env stays honoured for installs that set it before this tab existed —
    but an explicit choice in the UI has to win, or the tab does nothing."""
    monkeypatch.setenv("ODYSSEUS_TOOL_OUTPUT_INLINE_LIMIT", "1234")

    assert cp.resolve(CODEX, "gpt-5.4", 400_000)["tool_output_inline_limit"] == 1234

    stored[cp.profile_key(CODEX, "gpt-5.4")] = {
        "preset": "custom", "values": {"tool_output_inline_limit": 7777},
    }
    assert cp.resolve(CODEX, "gpt-5.4", 400_000)["tool_output_inline_limit"] == 7777


def test_custom_values_fall_back_per_knob(stored):
    """Setting one field must not silently zero the others."""
    stored[cp.profile_key(CODEX, "gpt-5.4")] = {
        "preset": "custom", "values": {"tool_output_tail_chars": 2000},
    }
    resolved = cp.resolve(CODEX, "gpt-5.4", 400_000)
    assert resolved["tool_output_tail_chars"] == 2000
    assert resolved["tool_output_inline_limit"] == 8000, "unset knobs keep the recommended value"


def test_out_of_range_values_are_clamped_not_rejected():
    clean = cp.sanitize({"a|b": {"preset": "custom", "values": {
        "tool_output_inline_limit": 99_999_999,
        "tool_output_tail_chars": -5,
        "trim_target_ratio": 4.0,
    }}})
    values = clean["a|b"]["values"]
    assert values["tool_output_inline_limit"] == cp.KNOBS["tool_output_inline_limit"]["max"]
    assert values["tool_output_tail_chars"] == cp.KNOBS["tool_output_tail_chars"]["min"]
    assert values["trim_target_ratio"] == cp.KNOBS["trim_target_ratio"]["max"]


def test_junk_is_dropped_without_losing_the_rest():
    clean = cp.sanitize({
        "good|model": {"preset": "balanced"},
        "bad|preset": {"preset": "nonsense"},
        "not-a-dict": "balanced",
        "empty|custom": {"preset": "custom", "values": {"unknown_knob": 1}},
    })
    assert set(clean) == {"good|model"}


def test_the_offload_reads_the_resolved_profile(tmp_path, monkeypatch):
    """End to end: a profile value has to actually change what gets offloaded."""
    import src.constants as constants
    from src import tool_output_store as tos

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    body = "line\n" * 1200  # ~6k chars

    kept, record = tos.maybe_offload(body, tool="bash", profile={"tool_output_inline_limit": 8000})
    assert record is None and kept == body

    trimmed, record = tos.maybe_offload(body, tool="bash", profile={"tool_output_inline_limit": 2000})
    assert record is not None and len(trimmed) < len(body)


def test_describe_exposes_ranges_and_help_for_every_knob(stored):
    described = cp.describe(CODEX, "gpt-5.4", 400_000)
    assert described["recommended"] == "long_context"
    assert set(described["knobs"]) == set(cp.KNOBS)
    for name, spec in described["knobs"].items():
        assert spec.get("label") and spec.get("help"), name
        assert "type" not in spec, "the python type must not leak to the client"
