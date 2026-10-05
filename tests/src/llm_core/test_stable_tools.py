"""A chat's tool list stays byte-identical so the prompt cache survives.

On 2026-09-27..29 a changed tool list was behind ~7.3M of 21M uncached input
tokens: every change made OpenAI re-read a long chat's whole history. On
GPT-5.6+ the tools array is now the chat's declared list, unchanged, and the
round's selection goes in `tool_choice: {"type": "allowed_tools"}`.
"""

import asyncio
import json

import pytest

import src.agent_loop as al
from src import constants, stable_tools
from src.llm_core import _build_chatgpt_responses_payload, _remember_rejected_param

CHATGPT = "https://chatgpt.com/backend-api/codex/responses"


def _schema(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    stable_tools.reset_for_tests()
    yield
    stable_tools.reset_for_tests()


# ── which routes ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,ok", [
    ("gpt-6-luna", True), ("gpt-6-sol", True), ("gpt-5.6-sol", True), ("gpt-6.1-sol", True),
    ("gpt-5.5", False), ("gpt-5", False), ("gpt-4o", False), ("qwen3-coder", False),
])
def test_only_gpt_5_6_and_later(model, ok):
    assert stable_tools.model_supported(model) is ok


def test_only_the_chatgpt_responses_route(monkeypatch):
    assert stable_tools.route_supported(CHATGPT, "gpt-6-luna")
    assert not stable_tools.route_supported("https://api.openai.com/v1", "gpt-6-luna")
    assert not stable_tools.route_supported("http://192.168.1.68:8086/v1", "gpt-6-luna")
    monkeypatch.setattr(stable_tools, "enabled", lambda: False)
    assert not stable_tools.route_supported(CHATGPT, "gpt-6-luna")


# ── the declared list ────────────────────────────────────────────────────────

def _names(schemas):
    return [stable_tools._name(s) for s in schemas]


def test_the_declared_list_only_grows_and_keeps_its_order():
    sent, callable_ = stable_tools.declare("c1", [_schema("read_file"), _schema("bash")])
    assert _names(sent) == ["read_file", "bash"] and callable_ == ["read_file", "bash"]
    # A narrower round: the same tools are sent, fewer are callable.
    sent, callable_ = stable_tools.declare("c1", [_schema("bash")])
    assert _names(sent) == ["read_file", "bash"] and callable_ == ["bash"]
    # A new tool joins at the end.
    sent, callable_ = stable_tools.declare("c1", [_schema("web_search"), _schema("read_file")])
    assert _names(sent) == ["read_file", "bash", "web_search"]
    assert callable_ == ["web_search", "read_file"]
    # A tool-free wrap-up round still sends everything, with nothing callable.
    sent, callable_ = stable_tools.declare("c1", [])
    assert _names(sent) == ["read_file", "bash", "web_search"] and callable_ == []


def test_a_changed_parameter_structure_replaces_its_entry_in_place():
    stable_tools.declare("c1", [_schema("a"), _schema("b")])
    changed = _schema("a")
    changed["function"]["parameters"] = {"type": "object", "properties": {"q": {"type": "string"}}}
    sent, _ = stable_tools.declare("c1", [changed])
    assert _names(sent) == ["a", "b"]
    assert "q" in sent[0]["function"]["parameters"]["properties"]


def test_a_description_only_change_keeps_the_declared_bytes():
    """2026-10-02: an MCP identity label / refreshed description changed a declared
    tool at the same count (tools hash e0dd1d7baf -> a674d8987f, 0% cache on 68k)."""
    first = [_schema("a", desc="[MCP:srv] one"), _schema("b")]
    sent1, _ = stable_tools.declare("c1", first)
    sent2, _ = stable_tools.declare("c1", [_schema("a", desc="[MCP:srv (me@x)] one, reworded"), _schema("b")])
    assert json.dumps(sent1, sort_keys=True) == json.dumps(sent2, sort_keys=True)
    sent3, _ = stable_tools.declare("c1", [_schema("c")])
    assert _names(sent3) == ["a", "b", "c"]
    assert sent3[0]["function"]["description"] == "[MCP:srv] one"


def test_past_the_cap_it_starts_over_from_the_round(monkeypatch):
    monkeypatch.setattr(stable_tools, "MAX_DECLARED", 3)
    stable_tools.declare("c1", [_schema("a"), _schema("b"), _schema("c")])
    sent, _ = stable_tools.declare("c1", [_schema("d"), _schema("a")])
    assert _names(sent) == ["d", "a"]


def test_a_restart_sends_the_same_bytes():
    first, _ = stable_tools.declare("c1", [_schema("read_file"), _schema("bash")])
    stable_tools.reset_for_tests()          # the process restarted
    again, _ = stable_tools.declare("c1", [_schema("bash")])
    assert json.dumps(again) == json.dumps(first)
    assert stable_tools.preview("c1", ["web_search"]) == {"read_file", "bash", "web_search"}


# ── the payload ──────────────────────────────────────────────────────────────

def _payload(tools, allowed, model="gpt-6-luna"):
    return _build_chatgpt_responses_payload(
        model, [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], 0.2, 0,
        tools=tools, target_url_hint=CHATGPT, cache_key="c1", allowed_tools=allowed)


def test_the_payload_keeps_the_tools_and_narrows_with_allowed_tools():
    declared = [_schema("read_file"), _schema("bash"), _schema("web_search")]
    payload = _payload(declared, ["bash", "not_declared"])
    assert [t["name"] for t in payload["tools"]] == ["read_file", "bash", "web_search"]
    assert payload["tool_choice"] == {"type": "allowed_tools", "mode": "auto",
                                      "tools": [{"type": "function", "name": "bash"}]}
    assert _payload(declared, [])["tool_choice"] == "none"
    assert "tool_choice" not in _payload(declared, None)


def test_a_backend_that_refused_tool_choice_gets_only_the_callable_tools():
    _remember_rejected_param(CHATGPT, "gpt-6-terra", "tool_choice", "Unsupported parameter: tool_choice")
    payload = _payload([_schema("read_file"), _schema("bash")], ["bash"], model="gpt-6-terra")
    assert [t["name"] for t in payload["tools"]] == ["bash"] and "tool_choice" not in payload


# ── the loop ─────────────────────────────────────────────────────────────────

def _run_turn(monkeypatch, url, model, relevant, session_id="chat-stable"):
    import core.database as db

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(db, "get_session_settings", lambda sid, **_k: {})
    seen = []

    async def _fake_stream(_candidates, msgs, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        request = await factory(0, url, model, {}) if factory else {}
        seen.append({"top": kwargs, "candidate": (request or {}).get("kwargs") or {},
                     "messages": [dict(m) for m in msgs]})
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _go():
        return [c async for c in al.stream_agent_loop(
            url, model, [{"role": "user", "content": "check the repo"}], max_rounds=2,
            relevant_tools=set(relevant), session_id=session_id)]

    asyncio.run(_go())
    return seen[0]


def test_a_gpt6_chatgpt_turn_sends_the_declared_list_with_allowed_tools(monkeypatch):
    first = _run_turn(monkeypatch, CHATGPT, "gpt-6-luna", {"read_file", "grep"})
    for kwargs in (first["top"], first["candidate"]):
        assert kwargs["allowed_tools"] is not None
        assert {"read_file", "grep"} <= set(kwargs["allowed_tools"])
        assert set(kwargs["allowed_tools"]) <= set(_names(kwargs["tools"]))
    notes = [m for m in first["messages"] if "Tools you can call this turn" in str(m.get("content"))]
    assert notes and "`grep`" in notes[0]["content"]


def test_the_system_prompt_does_not_change_when_a_turn_narrows(monkeypatch):
    first = _run_turn(monkeypatch, CHATGPT, "gpt-6-luna", {"read_file", "grep", "web_search"})
    second = _run_turn(monkeypatch, CHATGPT, "gpt-6-luna", {"read_file"})
    system = [m["content"] for m in first["messages"] if m.get("role") == "system"]
    assert system == [m["content"] for m in second["messages"] if m.get("role") == "system"]
    assert _names(first["top"]["tools"]) == _names(second["top"]["tools"])[:len(_names(first["top"]["tools"]))]


def test_other_routes_are_unchanged(monkeypatch):
    other = _run_turn(monkeypatch, "https://api.openai.com/v1", "gpt-6-luna", {"read_file", "grep"},
                      session_id="chat-other")
    assert other["top"]["allowed_tools"] is None and other["candidate"]["allowed_tools"] is None
    assert not any("Tools you can call this turn" in str(m.get("content")) for m in other["messages"])


def test_a_new_mcp_tool_brings_its_server_group_in_the_same_change():
    """2026-10-02: one file server's tools arrived in three requests within a
    minute, each a full uncached re-read of the prompt."""
    files = [_schema(f"mcp__files__{n}") for n in ("list_files", "get_file", "get_file_libraries")]

    def group(new_names):
        return files if any(n.startswith("mcp__files__") for n in new_names) else []

    stable_tools.declare("s1", [_schema("read_file")], group=group)
    declared, allowed = stable_tools.declare("s1", [_schema("read_file"), files[0]], group=group)
    names = [s["function"]["name"] for s in declared]
    assert names == ["read_file", "mcp__files__list_files", "mcp__files__get_file", "mcp__files__get_file_libraries"]
    assert allowed == ["read_file", "mcp__files__list_files"]

    again, _ = stable_tools.declare("s1", [_schema("read_file"), files[2]], group=group)
    assert again == declared  # nothing new: the tool block stays byte-identical


def test_a_group_that_raises_is_ignored():
    def boom(_names):
        raise RuntimeError("x")

    declared, _ = stable_tools.declare("s2", [_schema("mcp__x__a")], group=boom)
    assert [s["function"]["name"] for s in declared] == ["mcp__x__a"]
