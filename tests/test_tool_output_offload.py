"""Oversized tool results are offloaded instead of riding along in context.

A tool result is replayed to the model on every remaining round of the turn, so
a single big log dump is charged once per round that follows it. The store
keeps the full text on disk, indexes it for retrieval, and leaves an excerpt
that names the reference `recall_tool_output` reads back.

The vector index is deliberately NOT required here: these tests run with no
ChromaDB, which is exactly the degraded mode the store has to survive.
"""
import asyncio
import os
import tempfile

import pytest


@pytest.fixture()
def store(monkeypatch):
    """A tool_output_store rooted in a temp DATA_DIR."""
    tmp = tempfile.mkdtemp()
    import src.constants as constants
    import src.tool_output_store as tos

    monkeypatch.setattr(constants, "DATA_DIR", tmp, raising=False)
    return tos


def test_small_output_is_untouched(store):
    text = "### ls\n" + "file.txt\n" * 10
    out, record = store.maybe_offload(text, tool="ls")
    assert out == text
    assert record is None


def test_large_output_is_replaced_by_an_excerpt(store):
    body = "\n".join(f"line {i}: something happened" for i in range(4000))
    out, record = store.maybe_offload(body, tool="bash", command="cat big.log")

    assert record is not None
    assert record["chars"] == len(body)
    # The point of the exercise: what goes to the model is much smaller.
    assert len(out) < len(body) / 4
    # Head and tail both survive — the shape of the output and its last line
    # are where the answer usually is.
    assert "line 0: something happened" in out
    assert "line 3999: something happened" in out
    # And the model is told how to get the rest.
    assert record["ref"] in out
    assert "recall_tool_output" in out


def test_full_text_is_recoverable_by_ref(store):
    body = "\n".join(f"row {i}" for i in range(3000))
    _out, record = store.maybe_offload(body, tool="bash")
    assert store.load(record["ref"]) == body


def test_search_falls_back_to_keyword_scan_without_chromadb(store):
    body = "\n".join(
        ["boring filler line"] * 500
        + ["ERROR: dhcp lease renewal failed for 10.0.0.42"]
        + ["boring filler line"] * 500
    )
    _out, record = store.maybe_offload(body, tool="bash")

    hits = store.search("dhcp lease renewal", ref=record["ref"])
    assert hits, "keyword fallback should find the line when the vector index is down"
    assert any("dhcp lease renewal failed" in h["text"] for h in hits)


def test_unknown_ref_is_rejected_not_guessed(store):
    assert store.load("not-a-ref") is None
    assert store.is_ref("toolout-0123456789") is True
    assert store.is_ref("toolout-zz") is False


def _run(coro):
    return asyncio.run(coro)


def test_recall_tool_reads_a_slice_in_order(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    body = "".join(f"{i:05d}-" for i in range(2000))
    _out, record = store.maybe_offload(body, tool="bash")

    result = _run(RecallToolOutputTool().execute(
        '{"ref": "%s", "offset": 0, "limit": 300}' % record["ref"], {"session_id": "s1"}
    ))
    assert "error" not in result
    assert body[:300] in result["results"]
    # It tells the agent where to continue rather than leaving it to guess.
    assert '"offset": 300' in result["results"]


def test_recall_tool_rejects_a_ref_that_is_not_one(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    result = _run(RecallToolOutputTool().execute(
        '{"ref": "/etc/passwd"}', {"session_id": "s1"}
    ))
    assert result.get("exit_code") == 1
    assert "not a stored-output reference" in result["error"]


def test_recall_tool_lists_what_is_stored_when_asked_for_nothing(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    body = "x" * 20000
    _out, record = store.maybe_offload(body, tool="bash", session_id="s1", command="cat x")

    result = _run(RecallToolOutputTool().execute("{}", {"session_id": "s1"}))
    assert record["ref"] in result["results"]


def test_expired_output_reports_instead_of_pretending(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    body = "y" * 20000
    _out, record = store.maybe_offload(body, tool="bash")
    txt_path, _meta = store._paths(record["ref"])
    os.remove(txt_path)

    result = _run(RecallToolOutputTool().execute(
        '{"ref": "%s", "offset": 0}' % record["ref"], {"session_id": "s1"}
    ))
    assert result.get("exit_code") == 1
    assert "no longer stored" in result["error"]


def test_a_source_file_read_is_not_cut_in_half(store):
    """`edit_file` matches an exact string against what `read_file` returned.

    Trimming the middle out of a source file would turn a working edit into a
    failed one, so file reads get much more room than a log dump does.
    """
    source = "\n".join(f"    line {i} = compute({i})" for i in range(400))
    assert len(source) > store.DEFAULT_INLINE_LIMIT

    out, record = store.maybe_offload(source, tool="read_file")
    assert record is None
    assert out == source


def test_a_huge_read_still_offloads(store):
    body = "x" * 60_000
    out, record = store.maybe_offload(body, tool="read_file")
    assert record is not None
    assert len(out) < len(body) / 4
