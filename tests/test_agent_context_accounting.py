"""Native tool schemas and cache usage must be visible in context metrics."""

from src.agent_loop import _compute_final_metrics, _estimate_tool_schema_tokens
from src.model_context import estimate_tokens


def test_schema_estimate_adds_real_request_overhead():
    messages = [{"role": "user", "content": "hello"}]
    schemas = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from disk",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }]

    schema_tokens = _estimate_tool_schema_tokens(schemas)
    assert schema_tokens > 0
    assert estimate_tokens(messages) + schema_tokens > estimate_tokens(messages)


def test_final_metrics_report_cache_and_schema_breakdown():
    metrics = _compute_final_metrics(
        [{"role": "user", "content": "hello"}],
        "done",
        1.0,
        0.2,
        400_000,
        real_input_tokens=1_200,
        real_output_tokens=40,
        has_real_usage=True,
        tool_events=[],
        round_texts=[],
        request_context_tokens=1_500,
        cached_input_tokens=900,
        cache_write_input_tokens=100,
        tool_schema_tokens=300,
    )

    assert metrics["cached_input_tokens"] == 900
    assert metrics["cache_write_input_tokens"] == 100
    assert metrics["uncached_input_tokens"] == 300
    assert metrics["tool_schema_tokens"] == 300
    assert metrics["request_context_tokens"] == 1_500

