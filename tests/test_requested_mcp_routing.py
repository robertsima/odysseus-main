"""Naming a connected MCP server attaches its read-only tools.

Observed 2026-09-24: Penpot (81 tools, over the always-bound cap, so gated)
was connected and green, the user asked about it, and retrieval offered only
delete_team / update_team -- cosine similarity has no preference for reads.
The model probed the server with update_team. The fork's rule, "a mentioned
server gets its read-only tools, writes only through explicit bindings"
(`McpManager.discover_requested_tools`), lost its only caller in the
upstream sync (c72cb40c). These tests drive the real loop with a real
manager holding a Penpot-shaped catalog; only the model stream, the tool
index and the settings are fake.
"""

import asyncio
import collections
import json

import src.agent_loop as al
import src.mcp_manager as mm
import src.tool_index as tool_index
from src.mcp_manager import McpManager

SERVER = "c5ec6d7a"
READS = ["get_profile", "list_teams", "list_projects", "get_file", "get_page_shapes"]
WRITES = ["update_team", "delete_team", "delete_team_invitation", "create_rectangle",
          "update_shape", "delete_webhook"]


def _q(name):
    return f"mcp__{SERVER}__{name}"


QUALIFIED_READS = {_q(n) for n in READS}
QUALIFIED_WRITES = {_q(n) for n in WRITES}


class _Penpot(McpManager):
    """The real manager, recording what the loop asked the requested-tool path."""

    def __init__(self):
        super().__init__()
        self._tools = {SERVER: [
            {"name": n, "description": n.replace("_", " "), "input_schema": {}} for n in READS + WRITES
        ]}
        self._connections = {SERVER: {"status": "connected", "name": "Penpot", "identity": ""}}
        self.requests = []

    def discover_requested_tools(self, query, **kwargs):
        result = super().discover_requested_tools(query, **kwargs)
        self.requests.append((query, kwargs, set(result)))
        return result


class _Index:
    """Retrieval that picks what the incident's did: a destructive neighbour."""

    def __init__(self, extra=()):
        self._extra = set(extra)

    def index_mcp_tools(self, *_a, **_k):
        return None

    def mcp_index_state(self, *_a, **_k):
        return {}

    def get_tools_for_query(self, *_a, **_k):
        return set(tool_index.ALWAYS_AVAILABLE) | self._extra


def _setup(monkeypatch, *, retrieved=(), session_settings=None):
    mgr = _Penpot()
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: mgr, raising=False)
    monkeypatch.setattr(al, "_load_mcp_disabled_map", lambda: {}, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al, "_STICKY_TOOLS", collections.OrderedDict())
    # Pin the default caps so a local settings file cannot bind Penpot outright.
    monkeypatch.setattr(mm, "_always_bound_limits", lambda: (8, 24))
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: _Index(retrieved))
    if session_settings is not None:
        import core.database as db
        monkeypatch.setattr(db, "get_session_settings", lambda sid, **_k: dict(session_settings))

    async def _fake_exec(block, *a, **k):
        return block.tool_type, {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    return mgr


def _run(monkeypatch, text, history=(), **kwargs):
    """Round-1 tool names (None when the round was sent no tools at all)."""
    sent = []

    async def _fake_stream(_candidates, messages, **kw):
        tools = kw.get("tools")
        sent.append(None if tools is None else {(t.get("function") or {}).get("name") for t in tools})
        yield f'data: {json.dumps({"delta": "Checked."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _drain():
        return [c async for c in al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o",
            [*history, {"role": "user", "content": text}], max_rounds=1, **kwargs,
        )]

    asyncio.run(_drain())
    return sent[0]


def _penpot(names):
    return {n for n in names or () if n.startswith(f"mcp__{SERVER}__")}


def test_naming_the_server_attaches_its_read_tools_and_no_write(monkeypatch):
    mgr = _setup(monkeypatch, retrieved={_q("delete_team")})
    sent = _run(monkeypatch, "check that penpot works")
    # A first message this short reads as low-signal; naming a connected
    # server must still take the tool path rather than the tool-free reply.
    assert sent is not None
    assert {_q("get_profile"), _q("list_teams")} <= sent
    # Every Penpot tool in the round is a read, except the one retrieval
    # picked on its own: this path added no write.
    assert _penpot(sent) - {_q("delete_team")} == QUALIFIED_READS
    # Asked twice: once to keep the turn off the direct reply, once to attach.
    assert len(mgr.requests) == 2
    for query, kwargs, result in mgr.requests:
        assert query == "check that penpot works"
        assert result == QUALIFIED_READS
        assert kwargs["enabled_tools"] is None
        assert kwargs["readonly"] is False


def test_a_retry_inherits_the_server_its_previous_message_named(monkeypatch):
    """"try again" names nothing; on 2026-09-24 retrieval scored those words
    and the retry got delete_team back. It inherits the previous message's
    server, as a short follow-up inherits a named tool."""
    _setup(monkeypatch, retrieved={_q("delete_team")})
    sent = _run(monkeypatch, "try again", history=[
        {"role": "user", "content": "check that penpot works"},
        {"role": "assistant", "content": "Penpot's server could not reach the Penpot API."},
    ])
    assert sent is not None
    assert {_q("get_profile"), _q("list_teams")} <= sent
    assert _penpot(sent) - {_q("delete_team")} == QUALIFIED_READS


def test_a_message_not_naming_the_server_adds_nothing(monkeypatch):
    mgr = _setup(monkeypatch)
    sent = _run(monkeypatch, "Search the web for the latest Python release notes")
    assert sent is not None and "web_search" in sent
    assert not _penpot(sent)
    assert [result for _q_, _kw, result in mgr.requests] == [set()]


def test_a_plain_low_signal_first_message_keeps_the_direct_reply(monkeypatch):
    _setup(monkeypatch)
    assert _run(monkeypatch, "Explain stable sorting algorithms.") is None


def test_plan_mode_asks_for_reads_only(monkeypatch):
    mgr = _setup(monkeypatch)
    sent = _run(monkeypatch, "check that penpot works", plan_mode=True)
    assert mgr.requests and all(kwargs["readonly"] is True for _q_, kwargs, _r in mgr.requests)
    assert {_q("get_profile"), _q("list_teams")} <= sent
    assert not sent & QUALIFIED_WRITES


def test_a_read_only_worker_asks_for_reads_only(monkeypatch):
    mgr = _setup(monkeypatch, session_settings={"workflow_readonly": True})
    sent = _run(monkeypatch, "Check the Penpot MCP server works", session_id="ro-worker")
    assert mgr.requests and all(kwargs["readonly"] is True for _q_, kwargs, _r in mgr.requests)
    assert {_q("get_profile"), _q("list_teams")} <= sent


def test_a_server_the_chat_may_not_reach_attaches_nothing(monkeypatch):
    mgr = _setup(monkeypatch, session_settings={"allowed_mcp_servers": ["some-other-server"]})
    sent = _run(monkeypatch, "Check the Penpot MCP server works", session_id="gated-chat")
    assert mgr.requests and mgr.requests[-1][1]["allowed_servers"] == ["some-other-server"]
    assert mgr.requests[-1][2] == set()
    assert not _penpot(sent)


def test_a_pinned_role_is_left_as_its_policy_pinned_it(monkeypatch):
    mgr = _setup(monkeypatch, session_settings={
        "tool_access": "selected", "enabled_tools": ["read_file", _q("get_profile")],
    })
    sent = _run(monkeypatch, "Check the Penpot MCP server works", session_id="pinned-role")
    assert mgr.requests == []
    assert _q("get_profile") in sent
    assert _q("list_teams") not in sent


# Playwright-shaped: it annotates its snapshot/screenshot tools read-only. More
# than the 8-tool always-bound cap, so only something attaching them sends them.
BROWSER_READS = {"browser_snapshot", "browser_take_screenshot", "browser_console_messages",
                 "browser_network_requests"}
BROWSER_WRITES = {"browser_click", "browser_evaluate", "browser_run_code", "browser_file_upload",
                  "browser_navigate", "browser_type", "browser_drag", "browser_close"}


def _with_browser(mgr):
    mgr._connections["builtin_browser"] = {"status": "connected", "name": "Built-in: Browser", "identity": ""}
    mgr._tools["builtin_browser"] = [
        {"name": n, "description": n.replace("_", " "), "input_schema": {},
         "annotations": {"readOnlyHint": n in BROWSER_READS, "destructiveHint": n not in BROWSER_READS}}
        for n in sorted(BROWSER_READS | BROWSER_WRITES)
    ]
    return mgr


def test_mentioning_a_browser_attaches_no_browser_tools(monkeypatch):
    """"builtin_browser" minus the generic "builtin" is "browser". A passing
    mention used to attach the browser's reads, which made the browser
    expansion add its whole catalogue, click/evaluate/run_code included, and
    took a low-signal first message off the tool-free reply."""
    _with_browser(_setup(monkeypatch))
    assert _run(monkeypatch, "my browser keeps crashing") is None
    assert _run(monkeypatch, "Explain how a browser renders CSS") is None
    sent = _run(monkeypatch, "Which browser is best for web development?")
    assert sent is not None
    assert not {n for n in sent if n.startswith("mcp__builtin_browser__")}
