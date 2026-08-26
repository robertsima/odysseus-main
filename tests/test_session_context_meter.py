"""What the header context pill counts.

The pill gates the compaction warning, so it has to measure roughly what the
agent's trimmer measures. Estimating the transcript alone misses the system
prompt, the tool schemas and every tool result (those live in message metadata,
which `estimate_tokens` does not read) — so a tool-heavy session read as
comfortable while the real prompt was near the budget.
"""
import pytest

from routes.history.history_routes import _prompt_overhead_tokens, _trim_budget_tokens
from src.model_context import estimate_tokens


def _turn(role, text, **meta):
    entry = {"role": role, "content": text}
    if meta:
        entry["metadata"] = meta
    return entry


def test_no_measured_turn_reports_transcript_only():
    """A fresh chat must behave exactly as it did before."""
    messages = [_turn("user", "hello"), _turn("assistant", "hi")]
    history, overhead, measured, window = _prompt_overhead_tokens(messages)
    assert history == estimate_tokens(messages)
    assert (overhead, measured, window) == (0, 0, 0)


def test_overhead_is_the_gap_between_the_transcript_and_the_real_prompt():
    messages = [
        _turn("user", "run the thing"),
        _turn("assistant", "done", request_context_tokens=9000, context_length=128_000),
    ]
    transcript = estimate_tokens(messages)
    history, overhead, measured, window = _prompt_overhead_tokens(messages)
    assert measured == 9000
    assert window == 128_000
    assert overhead == 9000 - transcript
    assert history + overhead == 9000, "right after a turn the total is the measured prompt"


def test_overhead_survives_a_new_user_message():
    """Overhead is measured as of the turn, not against the grown transcript."""
    measured_at = [
        _turn("user", "run the thing"),
        _turn("assistant", "done", request_context_tokens=9000, context_length=128_000),
    ]
    _, overhead_before, _, _ = _prompt_overhead_tokens(measured_at)

    grown = measured_at + [_turn("user", "x" * 4000)]
    history, overhead_after, _, _ = _prompt_overhead_tokens(grown)

    assert overhead_after == overhead_before, "a new user turn must not eat the overhead"
    assert history + overhead_after > 9000, "the new message has to push usage up, not down"


def test_the_most_recent_measured_turn_wins():
    messages = [
        _turn("user", "a"),
        _turn("assistant", "b", request_context_tokens=5000, context_length=8192),
        _turn("user", "c"),
        _turn("assistant", "d", request_context_tokens=7000, context_length=128_000),
    ]
    _, _, measured, window = _prompt_overhead_tokens(messages)
    assert (measured, window) == (7000, 128_000)


@pytest.mark.parametrize("bad", [None, "", 0, -5, "nonsense", {}])
def test_junk_metrics_fall_back_instead_of_raising(bad):
    messages = [
        _turn("user", "a"),
        _turn("assistant", "b", request_context_tokens=bad, context_length=bad),
    ]
    history, overhead, measured, window = _prompt_overhead_tokens(messages)
    assert (overhead, measured, window) == (0, 0, 0)
    assert history == estimate_tokens(messages)


def test_a_transcript_larger_than_the_measurement_never_goes_negative():
    """Trimming can make a later prompt smaller than the stored transcript."""
    messages = [
        _turn("user", "x" * 40_000),
        _turn("assistant", "ok", request_context_tokens=100, context_length=8192),
    ]
    _, overhead, _, _ = _prompt_overhead_tokens(messages)
    assert overhead == 0


# ── The trim budget is not the context window ──────────────────────────


class _Session:
    endpoint_url = "http://localhost:11434/v1"
    model = "qwen"


def test_trim_budget_is_scaled_off_the_window_not_equal_to_it(monkeypatch):
    """The gap between these two is why the ring and the trim log disagree."""
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    budget = _trim_budget_tokens(_Session(), 128_000)
    assert 0 < budget < 128_000


def test_trim_budget_is_capped_for_a_very_long_window(monkeypatch):
    import src.settings as settings_mod
    from src.context_budget import DEFAULT_HARD_MAX

    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    assert _trim_budget_tokens(_Session(), 1_000_000) == DEFAULT_HARD_MAX


def test_trim_budget_reports_zero_rather_than_failing_the_endpoint(monkeypatch):
    import src.settings as settings_mod

    def _boom(key, default=None):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_mod, "get_setting", _boom)
    assert _trim_budget_tokens(_Session(), 128_000) == 0
