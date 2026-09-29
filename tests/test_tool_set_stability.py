"""A long chat on a large-window API model changes its tool list rarely.

The Codex Responses backend's cached prefix starts at the tools array, so any
change to the tool list is a full cache miss. On 2026-09-27 a gpt-6-luna chat
(400k window) grew its remembered tool set by 2-4 tools almost every turn and
restarted it at 48 (~307k uncached tokens over 8 turns), and discover_tools
changed it again mid-turn (~178k over 4 events). One more cached schema costs
~70 tokens a request; one growth re-sends 30-55k.

So on hosted API routes with a 128k+ window the cap is larger, the set grows
by whole domain chunks, a restart keeps the chat's most-used tools, and a
tool discovered mid-turn brings its domain siblings, appended at the END of
the tools array. Local routes and small windows keep the lean behaviour.
Policy still decides: nothing the chat may not use is ever added.
"""
import json
import random

import pytest

import src.agent_loop as al
from src.agent_loop import (
    FUNCTION_TOOL_SCHEMAS,
    _STICKY_CHUNK_GROUPS,
    _STICKY_TOOLS,
    _record_sticky_tool_use,
    _sticky_domain_chunk,
    _sticky_order_schemas,
    _sticky_route_window,
    _sticky_tool_cap,
    _sticky_tool_selection,
)
from src.tool_discovery import TurnToolDiscovery
from tests.test_same_turn_tool_attachment import _collect, _patch

ALL_NATIVE = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    from src import constants, stable_tools

    _STICKY_TOOLS.clear()
    # The ChatGPT route keeps a declared tool list per chat (src/stable_tools.py);
    # each test starts its chats without one.
    stable_tools.reset_for_tests()
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    yield
    _STICKY_TOOLS.clear()
    stable_tools.reset_for_tests()


# ── cap ────────────────────────────────────────────────────────────────────


def test_cap_scales_with_the_window_on_api_routes_only():
    assert _sticky_tool_cap(32_000, True) == (48, False)
    assert _sticky_tool_cap(128_000, True) == (96, True)
    assert _sticky_tool_cap(400_000, True) == (128, True)
    # A local/LAN route stays lean whatever its window.
    assert _sticky_tool_cap(400_000, False) == (48, False)
    assert _sticky_tool_cap(0, True) == (48, False)


def test_the_setting_replaces_the_cap(monkeypatch):
    monkeypatch.setattr(
        al, "get_setting",
        lambda key, default=None: 60 if key == "agent_sticky_tools_max" else default,
    )
    assert _sticky_tool_cap(400_000, True) == (60, True)
    assert _sticky_tool_cap(8_000, False) == (60, False)


def test_route_window_trusts_real_windows_not_the_default_fallback():
    window, api = _sticky_route_window("https://chatgpt.com/backend-api/codex/responses", "gpt-6-luna", 0)
    assert api is True and window >= 256_000
    # The bare DEFAULT_CONTEXT fallback proves nothing: an unknown model stays lean.
    assert _sticky_route_window("https://api.example.com/v1", "mystery-model", 128_000)[0] == 0
    # A window the caller resolved for real is used as is.
    assert _sticky_route_window("https://api.example.com/v1", "mystery-model", 200_000)[0] == 200_000
    assert _sticky_route_window("http://localhost:8080/v1", "gpt-6-luna", 400_000)[1] is False


# ── growth ─────────────────────────────────────────────────────────────────


def test_lean_growth_is_exactly_what_the_turn_selected():
    assert _sticky_tool_selection("s", {"read_file"}) == {"read_file"}
    assert _sticky_tool_selection("s", {"list_emails"}) == {"read_file", "list_emails"}


def test_chunked_growth_adds_the_whole_domain(caplog):
    import logging

    kw = dict(cap=128, chunk_permitted=ALL_NATIVE)
    with caplog.at_level(logging.INFO, logger="src.agent_loop"):
        first = _sticky_tool_selection("s", {"read_file"}, **kw)
        second = _sticky_tool_selection("s", {"read_file", "list_emails"}, **kw)
    assert {"grep", "glob", "edit_file", "write_file"} <= first
    assert {"read_email", "archive_email", "send_email"} <= second
    # The next email turn needs nothing new: no change, the prefix stays cached.
    assert _sticky_tool_selection("s", {"reply_to_email", "mark_email_read"}, **kw) == second
    line = [r.getMessage() for r in caplog.records if "grew by" in r.getMessage()][-1]
    assert "cap 128" in line and "domain chunk: email+" in line


def test_policy_denied_and_exempt_tools_never_join_a_chunk():
    permitted = ALL_NATIVE - {"send_email", "bulk_email"}
    out = _sticky_tool_selection(
        "s", {"list_emails", "create_document", "list_sessions"},
        disabled={"delete_email"}, cap=128, chunk_permitted=permitted,
    )
    assert not out & {"send_email", "bulk_email", "delete_email"}
    # Admin tools and the open-document editors join only when a request
    # names them or targets a document, never as a sibling.
    assert not out & {"edit_document", "update_document", "suggest_document", "manage_documents",
                      "create_session", "manage_session", "send_to_session"}
    assert "search_chats" in out and "read_email" in out


def test_the_shell_setting_gate_holds_for_chunks():
    # A chat whose Shell is Off gets no bash/python through a domain chunk.
    permitted = TurnToolDiscovery(FUNCTION_TOOL_SCHEMAS).permitted_names({"shell_access": "off"})
    chunk = _sticky_domain_chunk({"read_file"}, permitted)
    assert chunk["files"] and not chunk["files"] & {"bash", "python"}


def test_a_chunk_that_does_not_fit_is_left_out_whole():
    out = _sticky_tool_selection("s", {"a", "b", "list_emails"}, cap=6, chunk_permitted=ALL_NATIVE)
    assert out == {"a", "b", "list_emails"}


def test_a_chunked_restart_keeps_the_most_used_tools_and_this_turns_domains():
    kw = dict(cap=24, chunk_permitted=ALL_NATIVE)
    _sticky_tool_selection("s", {f"x{i}" for i in range(18)} | {"read_file"}, **kw)
    for _ in range(3):
        _record_sticky_tool_use("s", "x3")
    _record_sticky_tool_use("s", "read_file")
    out = _sticky_tool_selection("s", {"list_emails"} | {f"y{i}" for i in range(10)}, **kw)
    assert {"x3", "read_file"} <= out                   # most-used survive
    assert out & (_STICKY_CHUNK_GROUPS["email"] - {"list_emails"})  # this turn's domain rides along
    assert len(out) <= max(11, int(24 * al._STICKY_RESTART_FILL))  # room left to grow


# ── churn: multi-turn simulation across domains ────────────────────────────

_CORE = {"manage_memory", "ask_user", "update_plan", "recall_tool_output", "search_documents", "discover_tools"}
_UNGROUPED = ["read_app_logs", "inspect_runtime", "manage_git", "trigger_research", "manage_skills",
              "chat_with_model", "edit_image", "manage_wellbeing"]
_DOMAINS = ["files", "web", "email", "files", "notes_calendar_tasks", "web", "email", "documents",
            "files", "cookbook", "web", "email", "files", "notes_calendar_tasks", "git", "files",
            "web", "email", "documents", "files", "cookbook", "files", "email", "web", "sessions",
            "files", "notes_calendar_tasks", "web", "documents", "files"]


def _turns(seed):
    """What retrieval + domain seeding picks per turn: the ambient tools, 2-4
    tools of the turn's domain, and 0-2 tools no domain covers."""
    rng = random.Random(seed)
    out = []
    for domain in _DOMAINS:
        pool = sorted(_STICKY_CHUNK_GROUPS[domain])
        out.append(_CORE | set(rng.sample(pool, min(len(pool), rng.randint(2, 4))))
                   | set(rng.sample(_UNGROUPED, rng.randint(0, 2))))
    return out


def _changes(selections, **kw):
    """(tool-set changes after the first turn, uncached-token proxy): a change
    on turn i re-bills the prompt, taken as 30k + 5k per earlier turn."""
    _STICKY_TOOLS.clear()
    previous, changes, cost = None, 0, 0
    for i, selected in enumerate(selections):
        current = _sticky_tool_selection("sim", set(selected), offerable=ALL_NATIVE, **kw)
        for name in sorted(selected)[:2]:
            _record_sticky_tool_use("sim", name)
        if previous is not None and current != previous:
            changes += 1
            cost += 30_000 + 5_000 * i
        previous = set(current)
    return changes, cost


def test_a_multi_domain_chat_changes_its_tool_set_far_less_often():
    old = [0, 0]
    new = [0, 0]
    for seed in range(20):
        selections = _turns(seed)
        for total, kw in ((old, {}), (new, dict(cap=128, chunk_permitted=ALL_NATIVE))):
            changes, cost = _changes(selections, **kw)
            total[0] += changes
            total[1] += cost
    # Measured when written: 453 -> 205 changes over 20 chats x 29 turns, and
    # the re-billed-token proxy 2.3M -> 0.8M per chat (late, large prompts
    # almost never change any more).
    assert new[0] <= old[0] * 0.55
    assert new[1] <= old[1] * 0.45


# ── order: append-only ────────────────────────────────────────────────────


def _schemas(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def _names(schemas):
    return [s["function"]["name"] for s in schemas]


def test_a_tool_that_joins_later_goes_at_the_end():
    _sticky_tool_selection("s", {"discover_tools", "read_file", "grep"})
    assert _names(_sticky_order_schemas("s", _schemas("discover_tools", "read_file", "grep"))) == [
        "discover_tools", "read_file", "grep"]
    # `ls` sorts third in the canonical list; it is appended instead.
    assert _names(_sticky_order_schemas("s", _schemas("discover_tools", "read_file", "ls", "grep"))) == [
        "discover_tools", "read_file", "grep", "ls"]
    # Stable from then on, and no session means canonical order.
    assert _names(_sticky_order_schemas("s", _schemas("ls", "grep", "discover_tools", "read_file"))) == [
        "discover_tools", "read_file", "grep", "ls"]
    assert _names(_sticky_order_schemas(None, _schemas("b", "a"))) == ["b", "a"]


# ── discover_tools mid-turn, through the real loop ────────────────────────

URL = "https://chatgpt.com/backend-api/codex/responses"
MODEL = "gpt-6-luna"


class _FakeIndex:
    def get_tools_for_query(self, query, k):
        from src.tool_index import ALWAYS_AVAILABLE
        return set(ALWAYS_AVAILABLE) | {"read_file"}


def _discover_turn(monkeypatch, *, found, disabled_tools=None, rounds=None):
    sent = []

    def _result(block):
        if block.tool_type == "discover_tools":
            return {"output": "Loaded", "exit_code": 0, "loaded_names": list(found),
                    "continue_same_turn": True}
        return {"output": "ok", "exit_code": 0}

    _patch(monkeypatch, [], _result)
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: _FakeIndex())
    discover = [{"name": "discover_tools", "arguments": json.dumps({"query": "fetch a page"})}]
    rounds = rounds or [(None, discover), ("Done.", None)]

    async def _fake_stream(_candidates, _messages, **kwargs):
        idx = len(sent)
        sent.append(_names(kwargs.get("tools") or []))
        text, calls = rounds[min(idx, len(rounds) - 1)]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        if calls:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        URL, MODEL, [{"role": "user", "content": "Summarize the plan for the quarterly review"}],
        max_rounds=len(rounds), session_id="chat-stable", allow_private=False,
        # Not a caller-provided selection (that is never chunked); a forced
        # tool keeps the turn off the direct low-signal reply path.
        forced_tools={"read_file"}, disabled_tools=disabled_tools,
    ))
    return sent


def test_discovery_attaches_domain_siblings_at_the_end(monkeypatch):
    sent = _discover_turn(monkeypatch, found=["web_fetch"])
    first, second = sent[0], sent[1]
    assert "web_fetch" not in first and "web_search" not in first
    # One change carries the whole web domain, appended after what was sent.
    assert second[: len(first)] == first
    assert set(second[len(first):]) == {"web_fetch", "web_search"}
    # The next turn starts from exactly that list, in that order.
    next_turn = _discover_turn(monkeypatch, found=[], rounds=[("Hi.", None)])
    assert next_turn[0] == second


def test_discovery_never_attaches_a_sibling_the_policy_denies(monkeypatch):
    sent = _discover_turn(monkeypatch, found=["web_fetch"], disabled_tools={"web_search"})
    assert "web_fetch" in sent[1] and "web_search" not in sent[1]
