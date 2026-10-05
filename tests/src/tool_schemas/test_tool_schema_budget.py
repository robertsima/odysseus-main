"""Native tool descriptions must fit what compact_function_tool_schemas ships.

The compactor keeps whole leading sentences up to a per-tool and a
per-parameter budget. A description past the budget is silently cut, so the
text an author reviewed is not the text the model reads (2026-10-01 audit A2-1:
29 native tools lost the rule that came after the cut). These tests hold every
description inside the budget, so the clip is a no-op and the source is what
ships.
"""
import json

from src import tool_schemas as ts
from src.agent_tools import filesystem_tools
from src.constants import MAX_READ_CHARS

# manage_git and manage_agent_worktree are exempt from compaction because
# their action/field mapping must arrive whole; they get their own cap here.
_UNTRIMMED_CAP = 2000


def _described_params(node, path=""):
    """Yield (dotted path, description) for every parameter schema, nested ones included."""
    if isinstance(node, list):
        for child in node:
            yield from _described_params(child, path)
        return
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for name, child in props.items():
            if isinstance(child, dict) and isinstance(child.get("description"), str):
                yield f"{path}{name}", child["description"]
            yield from _described_params(child, f"{path}{name}.")
    yield from _described_params(node.get("items"), path)
    yield from _described_params(node.get("anyOf"), path)


def _native_functions():
    return [schema["function"] for schema in ts.FUNCTION_TOOL_SCHEMAS]


def test_every_tool_description_fits_its_budget():
    over = []
    for fn in _native_functions():
        cap = _UNTRIMMED_CAP if fn["name"] in ts._UNTRIMMED_TOOLS else ts._COMPACT_TOOL_DESCRIPTION_CHARS
        length = len(fn.get("description", ""))
        if length > cap:
            over.append(f"{fn['name']}: {length} > {cap}")
    assert not over, "Trim these descriptions, routing text first:\n" + "\n".join(over)


def test_every_parameter_description_fits_its_budget():
    over = []
    for fn in _native_functions():
        for path, text in _described_params(fn.get("parameters")):
            if len(text) > ts._COMPACT_PARAM_DESCRIPTION_CHARS:
                over.append(f"{fn['name']}.{path}: {len(text)} > {ts._COMPACT_PARAM_DESCRIPTION_CHARS}")
    assert not over, "Trim these parameter descriptions:\n" + "\n".join(over)


def test_compaction_changes_no_native_tool():
    """With every description inside the budget the clip returns the source text."""
    compact = ts.compact_function_tool_schemas(ts.FUNCTION_TOOL_SCHEMAS)
    changed = [
        a["function"]["name"]
        for a, b in zip(ts.FUNCTION_TOOL_SCHEMAS, compact)
        if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)
    ]
    assert not changed, f"compaction still rewrites: {changed}"


def _description(name):
    return next(fn["description"] for fn in _native_functions() if fn["name"] == name)


def test_file_tool_descriptions_state_the_real_limits():
    """The numbers in the prose are the constants the tools enforce."""
    assert f"{MAX_READ_CHARS:,}" in _description("read_file")
    assert str(filesystem_tools._CODENAV_MAX_HITS) in _description("grep")
    assert str(filesystem_tools._CODENAV_MAX_HITS) in _description("glob")
    assert str(filesystem_tools._CODENAV_MAX_HITS) in _description("ls")


def test_web_search_does_not_depend_on_a_tool_that_may_be_absent():
    text = _description("web_search")
    assert "trigger_research" in text and "when it is available" in text
    assert "use trigger_research instead" not in text


def test_workspace_text_matches_what_preflight_requires():
    """A chat with no workspace must pass one for a repository task; the parameter says so."""
    for tool in ("manage_agent_loadout", "send_to_session"):
        fn = next(f for f in _native_functions() if f["name"] == tool)
        text = fn["parameters"]["properties"]["workspace"]["description"]
        assert "Required when this chat has none" in text, tool
        assert "Omit to use this chat's workspace" not in text, tool


def test_memory_tool_has_one_description_in_both_places():
    """The native schema and the MCP server describe manage_memory with the same text."""
    import pytest

    memory_server = pytest.importorskip("mcp_servers.memory_server")
    assert memory_server.MANAGE_MEMORY_DESCRIPTION == _description("manage_memory")
