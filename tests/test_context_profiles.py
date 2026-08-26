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


# ── "All endpoints / All models" has to mean the global profile ─────────


def test_all_endpoints_all_models_is_the_global_key():
    """The tab's most general selection must land where `resolve` reads it."""
    assert cp.profile_key("", "") == cp.GLOBAL_KEY
    assert cp.profile_key(LOCAL, "") == f"{LOCAL}|"
    assert cp.profile_key(LOCAL, "qwen") == f"{LOCAL}|qwen"


def test_a_global_profile_reaches_a_specific_endpoint_and_model(stored):
    stored[cp.GLOBAL_KEY] = {"preset": "compact"}
    resolved = cp.resolve(CODEX, "gpt-5.4", 400_000)
    assert resolved["tool_output_inline_limit"] == cp.PRESETS["compact"]["values"]["tool_output_inline_limit"]
    assert resolved["_source"] == "global"


def test_a_legacy_pipe_key_still_applies(monkeypatch):
    """Anything already written under the old "|" key keeps working.

    Patches the settings read rather than `_stored_profiles`, because the
    normalisation lives in that one read path.
    """
    import src.settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_setting",
        lambda key, default=None: {"|": {"preset": "compact"}} if key == cp.SETTING_KEY else default,
    )
    assert cp.resolve(CODEX, "gpt-5.4", 400_000)["_source"] == "global"
    assert cp.sanitize({"|": {"preset": "compact"}}) == {cp.GLOBAL_KEY: {"preset": "compact"}}


def test_the_global_profile_is_named_as_global_not_as_a_model(stored):
    """A caller with no endpoint hits one key at three tiers; report the truth."""
    stored[cp.GLOBAL_KEY] = {"preset": "compact"}
    assert cp.resolve("", "")["_source"] == "global"


def test_a_more_specific_profile_still_beats_the_global_one(stored):
    stored[cp.GLOBAL_KEY] = {"preset": "compact"}
    stored[cp.profile_key(CODEX, "gpt-5.4")] = {"preset": "long_context"}
    resolved = cp.resolve(CODEX, "gpt-5.4", 400_000)
    assert resolved["_source"] == "model"
    assert resolved["tool_output_inline_limit"] == cp.PRESETS["long_context"]["values"]["tool_output_inline_limit"]


def test_describe_keys_an_unset_model_to_the_endpoint(stored):
    """What the tab loads and what Save writes have to be the same key."""
    stored[cp.profile_key(LOCAL, "")] = {"preset": "compact"}
    described = cp.describe(LOCAL, "", 8_192)
    assert described["key"] == cp.profile_key(LOCAL, "")
    assert described["selected"] == "compact", "the endpoint-wide profile must show as selected"


# ── The settings tab has to call the path the router actually serves ────
#
# The tab shipped calling /api/settings/context-profile while the handlers live
# on the auth router, which carries a /api/auth prefix — every load 404'd and
# the UI could only say "could not load context profiles". Nothing in Python
# catches that, because both halves are individually correct. So compare them.

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _router_paths(source: str) -> set:
    """Full paths served by routes/auth_routes.py, prefix included."""
    prefix = re.search(r'APIRouter\(prefix="([^"]*)"', source).group(1)
    return {
        prefix + path
        for path in re.findall(r'@router\.(?:get|post|put|delete)\("([^"]+)"', source)
    }


def _fetched_paths(source: str) -> set:
    """Paths the settings tab fetches for the context profile."""
    return set(re.findall(r"fetch\('(/api/[^'?]*context-profile)", source))


def test_settings_tab_calls_the_context_profile_route_that_exists():
    served = _router_paths((_REPO / "routes" / "auth_routes.py").read_text(encoding="utf-8"))
    called = _fetched_paths((_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8"))

    assert called, "the settings tab no longer fetches the context profile at all"
    assert called <= served, (
        f"settings.js fetches {sorted(called - served)}, which the auth router does not serve; "
        f"it serves {sorted(p for p in served if 'context-profile' in p)}"
    )
