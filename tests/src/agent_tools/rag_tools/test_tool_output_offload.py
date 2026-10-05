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


def test_github_mcp_json_gets_room_before_it_is_offloaded():
    """A GitHub search page or issue_read cut at 4k left one issue sliced
    mid-object and the agent spent rounds recalling the rest."""
    from src.tool_output_store import inline_limit

    assert inline_limit("mcp__github_read__search_issues") >= 16_000
    assert inline_limit("mcp__github_read__issue_read") >= 16_000
    assert inline_limit("mcp__other__thing") == inline_limit("")
    # A larger profile limit still wins over the per-prefix one.
    assert inline_limit("mcp__github_read__search_issues", {"tool_output_inline_limit": 30_000}) == 30_000


# ── Reading a stored result back ends the loop instead of feeding it ──────
#
# A turn recalled the same offloaded result about twelve times: every answer
# (a 3k slice, five 1.4k search chunks) looked partial, so the model asked
# again or re-ran the tool. A bare-ref read now returns the whole output, paged
# only when it is huge, and says when it has all been read.


def test_a_bare_ref_read_returns_the_whole_output(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    body = "\n".join(f"row {i}: value" for i in range(1000))  # ~15k chars
    _out, record = store.maybe_offload(body, tool="bash", command="cat rows")
    assert record is not None

    result = _run(RecallToolOutputTool().execute('{"ref": "%s"}' % record["ref"], {"session_id": "s1"}))
    assert body in result["results"]
    assert "complete stored output of bash" in result["results"]
    assert "do not recall this ref again" in result["results"]
    assert "remain" not in result["results"]


def test_a_huge_output_is_paged_and_each_page_names_the_next_offset(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool, _RECALL_FULL_CHARS

    body = "".join(f"{i:07d}\n" for i in range(6000))  # 48k chars
    _out, record = store.maybe_offload(body, tool="bash")
    ref = record["ref"]
    tool = RecallToolOutputTool()

    pages, offset = [], 0
    for _ in range(10):
        res = _run(tool.execute('{"ref": "%s", "offset": %d}' % (ref, offset), {}))["results"]
        pages.append(res)
        if f'"offset": ' not in res:
            break
        offset = int(res.rsplit('"offset": ', 1)[1].split("}", 1)[0])
    assert len(pages) == -(-len(body) // _RECALL_FULL_CHARS)  # ceil
    assert "Page forward like this; do not re-run bash" in pages[0]
    assert f"End of `{ref}`" in pages[-1]
    joined = "".join(p.split("\n\n", 1)[1].rsplit("\n\n[", 1)[0] for p in pages)
    assert joined == body


def test_a_query_on_an_output_that_fits_returns_all_of_it(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    body = "\n".join(["filler"] * 800 + ["needle: the port is 8443"] + ["filler"] * 800)
    _out, record = store.maybe_offload(body, tool="bash")
    res = _run(RecallToolOutputTool().execute(
        '{"ref": "%s", "query": "which port"}' % record["ref"], {}))["results"]
    assert body in res


def test_a_query_that_matches_nothing_in_a_huge_output_reads_it_in_order(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool

    body = "z" * 50_000
    _out, record = store.maybe_offload(body, tool="bash")
    res = _run(RecallToolOutputTool().execute(
        '{"ref": "%s", "query": "qwerty uiop"}' % record["ref"], {}))["results"]
    assert res.startswith('Nothing matched "qwerty uiop"; here is the output in order.')
    assert '"offset": 20000' in res


def test_an_explicit_limit_still_clamps(store):
    from src.agent_tools.rag_tools import RecallToolOutputTool, _RECALL_MAX_SLICE_CHARS

    body = "q" * 30_000
    _out, record = store.maybe_offload(body, tool="bash")
    res = _run(RecallToolOutputTool().execute(
        '{"ref": "%s", "limit": 50000}' % record["ref"], {}))["results"]
    assert f"characters 0-{_RECALL_MAX_SLICE_CHARS:,} of 30,000" in res


def test_the_truncation_note_names_the_tool_and_ref_before_the_head(store):
    body = "\n".join(f"line {i}" for i in range(5000))
    out, record = store.maybe_offload(body, tool="web_fetch", command="https://example.com")
    ref = record["ref"]
    first_line = out.split("\n", 1)[0]
    assert first_line.startswith("[Truncated web_fetch output")
    assert f'recall_tool_output {{"ref": "{ref}"}}' in first_line
    assert "do not re-run web_fetch" in first_line
    # The trailing note says the same, and still opens with the boilerplate the
    # ledger drops when it collapses the exchange.
    assert "This output was large, so only its head and tail are shown." in out
    assert f'recall_tool_output {{"ref": "{ref}"}}' in out.rsplit("This output was large", 1)[1]
    assert "Do NOT re-run web_fetch" in out


def test_the_recall_schema_says_to_page_not_rerun():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "recall_tool_output")
    desc = schema["function"]["description"]
    assert "whole stored output" in desc
    assert "offset" in desc and "rather than re-running the tool" in desc


def test_the_ledger_pointer_offers_the_bare_ref_read():
    from src.context_compactor import ledger_entry

    entry = ledger_entry("### bash: cat big.log\nsome facts", ["toolout-0123456789"])
    assert '{"ref": "toolout-0123456789"}' in entry
    assert "do NOT re-run the tool" in entry


def test_storing_many_outputs_does_not_walk_the_directory_each_time(store, monkeypatch):
    """2026-10-02: a ledger collapse stored 41-50 outputs in one second and each
    store() listed and stat'ed the whole directory on the event loop, blocking
    every chat for 4.45 s. Pruning is now throttled and runs on a worker."""
    store._last_prune_at = 0.0
    walks = []
    real_scandir, real_listdir, real_mtime = os.scandir, os.listdir, os.path.getmtime
    monkeypatch.setattr(os, "scandir", lambda *a, **k: (walks.append("scandir"), real_scandir(*a, **k))[1])
    monkeypatch.setattr(os, "listdir", lambda *a, **k: (walks.append("listdir"), real_listdir(*a, **k))[1])
    monkeypatch.setattr(os.path, "getmtime", lambda *a, **k: (walks.append("getmtime"), real_mtime(*a, **k))[1])

    for i in range(50):
        assert store.store(f"payload {i}\n" * 50, tool="bash")
    store._background().submit(lambda: None).result(timeout=10)

    assert len(walks) <= 2, walks  # one throttled prune, never one per store
    assert "getmtime" not in walks


def test_prune_runs_off_the_calling_thread(store):
    import threading

    store._last_prune_at = 0.0
    seen = []
    original = store._prune
    store._prune = lambda *a, **k: seen.append(threading.current_thread().name)
    try:
        store.store("x\n" * 100, tool="bash")
        store._background().submit(lambda: None).result(timeout=10)
    finally:
        store._prune = original
    assert seen and seen[0] != threading.current_thread().name
