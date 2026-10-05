"""Tool results that used to end in a dead end now name the next step.

2026-10-01 prompt audit A2-3, A2-4, A2-11, A2-12, A2-13, A2-15: a grep that
timed out dropped its hits, a truncated read gave no way to continue, a failed
edit said only "match exactly", refusals named no alternative, and web_fetch
rejected an SVG it could have returned.
"""
import asyncio
import importlib
import json
import os
from contextlib import contextmanager

import pytest

from src.agent_tools import ToolBlock, filesystem_tools as fs
from src.constants import MAX_READ_CHARS


@contextmanager
def _workspace(path):
    te = importlib.import_module("src.tool_execution")
    token = te._active_workspace.set(os.path.realpath(path))
    try:
        yield
    finally:
        te._active_workspace.reset(token)


def _run(coro):
    return asyncio.run(coro)


# -- A2-4: read_file ---------------------------------------------------------

def test_a_truncated_read_names_the_offset_to_continue_from(tmp_path):
    lines = [f"line {n} " + "x" * 90 for n in range(1, 600)]
    (tmp_path / "big.txt").write_text("\n".join(lines), encoding="utf-8")
    with _workspace(tmp_path):
        out = _run(fs.ReadFileTool().execute("big.txt", {}))["output"]
    assert f"truncated at {MAX_READ_CHARS} chars" in out
    marker = "call read_file again with offset="
    next_line = int(out.split(marker, 1)[1].split(" ", 1)[0])
    shown = out.split("\n... [truncated", 1)[0]
    # The cut can fall inside a line, so the named line is the last one shown: reading
    # from it returns that line whole and everything after.
    assert next_line == shown.count("\n") + 1
    assert f"line {next_line + 1} " not in shown


def test_a_truncated_ranged_read_names_the_offset_to_continue_from(tmp_path):
    lines = [f"line {n} " + "x" * 90 for n in range(1, 600)]
    (tmp_path / "big.txt").write_text("\n".join(lines), encoding="utf-8")
    with _workspace(tmp_path):
        out = _run(fs.ReadFileTool().execute(json.dumps({"path": "big.txt", "offset": 10, "limit": 500}), {}))["output"]
    next_line = int(out.split("call read_file again with offset=", 1)[1].split(" ", 1)[0])
    assert next_line > 10
    assert f"line {next_line - 1} " in out
    assert f"line {next_line} " not in out


def test_a_missing_file_says_how_to_find_it(tmp_path):
    with _workspace(tmp_path):
        result = _run(fs.ReadFileTool().execute("nope.txt", {}))
    assert result["exit_code"] == 1 and "glob or ls" in result["error"]


# -- A2-11: edit and patch failures ------------------------------------------

def test_edit_not_found_names_the_closest_line(tmp_path):
    (tmp_path / "a.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    with _workspace(tmp_path):
        result = _run(fs.EditFileTool().execute(json.dumps(
            {"path": "a.py", "old_string": "def totals(items):", "new_string": "x"}), {}))
    assert result["exit_code"] == 1
    assert "Closest line in the file is 1" in result["error"] and "def total(items):" in result["error"]


def test_edit_not_found_says_when_only_whitespace_differs(tmp_path):
    (tmp_path / "a.py").write_text("if x:\n    run()\n", encoding="utf-8")
    with _workspace(tmp_path):
        result = _run(fs.EditFileTool().execute(json.dumps(
            {"path": "a.py", "old_string": "if x:\n  run()", "new_string": "x"}), {}))
    assert "whitespace or indentation differs" in result["error"]


def test_patch_hunk_errors_say_what_to_change():
    ambiguous = "a\nb\na\nb\n"
    with pytest.raises(ValueError, match="matched 2 times: add unchanged lines"):
        fs._apply_patch_hunks(ambiguous, [[" a", " b"]], "f.py")
    with pytest.raises(ValueError, match="context not found: re-read the file"):
        fs._apply_patch_hunks("alpha\nbeta\n", [[" gamma", "+x"]], "f.py")


# -- A2-3: grep timeout ------------------------------------------------------

def test_a_grep_timeout_with_hits_returns_them_and_the_next_step():
    result = fs._grep_timeout_result(["/r/a.py:3:needle", "/r/b.py:9:needle"], "/r")
    assert result["exit_code"] == 0
    assert "/r/a.py:3:needle" in result["output"]
    assert "2 matches so far" in result["output"] and "Narrow `path`" in result["output"]


def test_a_grep_timeout_without_hits_is_an_error_with_the_next_step():
    result = fs._grep_timeout_result([], "/r")
    assert result["exit_code"] == 1
    assert result["error"].startswith("grep: timed out: the search of /r stopped after")
    assert "glob" in result["error"]


# -- A2-12 / A2-13: refusals -------------------------------------------------

def test_path_refusals_say_what_is_allowed(tmp_path):
    te = importlib.import_module("src.tool_execution")
    outside = tmp_path.parent
    with _workspace(tmp_path):
        with pytest.raises(ValueError, match=r"outside the workspace .*use a path inside the workspace"):
            te._resolve_tool_path(str(outside / "elsewhere.txt"))
    with pytest.raises(ValueError, match="get_workspace"):
        te._resolve_tool_path(os.path.abspath(os.sep + "definitely-outside-every-root.txt"))
    with pytest.raises(ValueError, match="ask the user to paste the part you need"):
        te._resolve_tool_path(os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa"))


@pytest.mark.parametrize("tool_name, expected", [
    ("not_a_tool_anywhere", "Call discover_tools"),
])
def test_unknown_tool_points_at_discovery(tool_name, expected):
    te = importlib.import_module("src.tool_execution")
    _desc, result = _run(te.execute_tool_block(
        ToolBlock(tool_name, "{}"), security_context=te.NO_TOOL_SECURITY_CONTEXT))
    assert expected in result["error"]


# -- A2-15: web_fetch and SVG -----------------------------------------------

def test_web_fetch_returns_svg_markup(monkeypatch):
    import src.search.content as content_mod
    from src.agent_tools import web_tools

    monkeypatch.setattr(content_mod, "fetch_webpage_content",
                        lambda url, **kw: {"content": "", "error": "", "title": "", "meta_description": ""})
    monkeypatch.setattr(web_tools, "_svg_body", lambda url, max_bytes: '<svg viewBox="0 0 1 1"><path d="M0 0"/></svg>')
    out = _run(web_tools.WebFetchTool().execute('{"url": "https://api.example.test/icons/helmet.svg"}', {}))
    assert out["exit_code"] == 0
    assert '<svg viewBox="0 0 1 1">' in out["output"]
    assert "Source: https://api.example.test/icons/helmet.svg" in out["output"]


def test_web_fetch_without_svg_still_fails_with_a_next_step(monkeypatch):
    import src.search.content as content_mod
    from src.agent_tools import web_tools

    monkeypatch.setattr(content_mod, "fetch_webpage_content",
                        lambda url, **kw: {"content": "", "error": "", "title": "", "meta_description": ""})
    monkeypatch.setattr(web_tools, "_svg_body", lambda url, max_bytes: "")
    out = _run(web_tools.WebFetchTool().execute('{"url": "https://api.example.test/icons/helmet.svg"}', {}))
    assert out["exit_code"] == 1 and "web_search" in out["error"]
