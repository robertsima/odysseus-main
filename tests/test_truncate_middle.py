"""Command output keeps its start and its end (src/tool_utils._truncate_middle).

A build or test run prints its failures and summary last. The bash tool kept
only the first 10k characters, so the model saw dependency downloads and not
whether the tests passed.
"""
from src.tool_utils import _truncate_middle


def test_short_text_is_unchanged():
    assert _truncate_middle("hello", limit=100) == "hello"
    assert _truncate_middle(None) == ""


def test_long_text_keeps_head_and_tail():
    text = "START" + "x" * 50_000 + "SUMMARY: 3 failed"
    out = _truncate_middle(text, limit=1000)
    assert out.startswith("START")
    assert out.endswith("SUMMARY: 3 failed")
    assert "chars omitted" in out
    assert len(out) < 1200
