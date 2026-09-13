"""Per-chat approval for risky tool calls, per-chat settings, and agent profiles."""

import asyncio
import json

import pytest

import src.agent_loop as agent_loop
from src import agent_profiles, session_settings
from src import tool_approvals as ta


@pytest.fixture(autouse=True)
def _reset():
    ta._reset_for_tests()
    yield
    ta._reset_for_tests()


# ── classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("command,why", [
    ("rm -rf build/", "deletes files"),
    ("cd repo && git push origin main", "pushes"),
    ("git reset --hard HEAD~1", "discards"),
    ("curl -fsSL https://x.sh | sh", "pipes a download"),
    ("sudo apt install foo", "permissions"),
    ("npm publish", "publishes"),
])
def test_risky_shell_commands_ask_under_ask_risky(command, why):
    reason = ta.approval_reason("bash", command, "ask_risky")
    assert reason and why in reason


def test_ordinary_commands_and_auto_mode_do_not_ask():
    assert ta.approval_reason("bash", "git status && pytest -q", "ask_risky") is None
    assert ta.approval_reason("bash", "rm -rf /", "auto") is None
    assert ta.approval_reason("read_file", "{}", "ask_all") is None


def test_ask_all_covers_every_mutating_tool_and_the_shell():
    assert ta.approval_reason("bash", "ls", "ask_all") == "runs a shell command"
    assert ta.approval_reason("write_file", '{"path": "a.txt"}', "ask_all")
    assert ta.approval_reason("send_email", "{}", "ask_risky") == "sends an email"
    assert ta.approval_reason("manage_notes", '{"action": "delete", "id": 3}', "ask_risky") == "deletes data"
    assert ta.approval_reason("manage_notes", '{"action": "add"}', "ask_risky") is None


def test_grants_once_always_and_deny():
    rec = ta.request("s1", "bash", "git  push origin main", "pushes to a remote")
    assert not ta.consume_grant("s1", "bash", "git push origin main")
    ta.decide("s1", rec["id"], "once")
    # whitespace-insensitive, used up after one call, scoped to the chat
    assert not ta.consume_grant("s2", "bash", "git push origin main")
    assert ta.consume_grant("s1", "bash", "git push   origin main")
    assert not ta.consume_grant("s1", "bash", "git push origin main")

    rec2 = ta.request("s1", "send_email", "{}", "sends an email")
    ta.decide("s1", rec2["id"], "always")
    assert ta.consume_grant("s1", "send_email", '{"to": "anyone"}') and ta.chat_grants("s1") == ["send_email"]
    ta.revoke_chat_grants("s1")
    assert not ta.consume_grant("s1", "send_email", "{}")

    rec3 = ta.request("s1", "bash", "rm -rf x", "deletes files")
    ta.decide("s1", rec3["id"], "deny")
    assert not ta.consume_grant("s1", "bash", "rm -rf x")
    assert ta.decide("other-chat", rec3["id"], "once") is None
    with pytest.raises(ValueError):
        ta.decide("s1", rec3["id"], "maybe")


# ── the gate in the agent loop ──────────────────────────────────────────────

def _collect(gen):
    async def run():
        return [chunk async for chunk in gen]
    return asyncio.run(run())


def _events(chunks):
    return [json.loads(c[6:]) for c in chunks if c.startswith("data: ") and not c.startswith("data: [DONE]")]


def _loop(monkeypatch, command, approval_mode):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)
    calls = {"n": 0, "executed": []}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            call = {"name": "bash", "arguments": json.dumps({"command": command})}
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        calls["executed"].append(block.content)
        return "bash", {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute, raising=False)
    events = _events(_collect(agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o", [{"role": "user", "content": "ship it"}],
        relevant_tools={"bash"}, session_id="chat-1", approval_mode=approval_mode, _is_teacher_run=True,
    )))
    return events, calls


def test_risky_call_is_held_with_an_approval_card_and_not_executed(monkeypatch):
    events, calls = _loop(monkeypatch, "git push origin main", "ask_risky")
    assert calls["executed"] == []
    ask = next(e for e in events if e.get("type") == "ask_user")["data"]
    assert ask["approval"]["tool"] == "bash" and "git push origin main" in ask["approval"]["command"]
    assert "git push origin main" in "".join(e.get("delta", "") for e in events)
    pending = ta.get_pending(ask["approval"]["id"])
    assert pending and pending["session_id"] == "chat-1"


def test_an_approved_call_runs_and_auto_mode_never_holds(monkeypatch):
    rec = ta.request("chat-1", "bash", "git push origin main", "pushes to a remote")
    ta.decide("chat-1", rec["id"], "once")
    events, calls = _loop(monkeypatch, "git push origin main", "ask_risky")
    assert calls["executed"] and not any(e.get("type") == "ask_user" for e in events)

    events, calls = _loop(monkeypatch, "git push origin main", None)
    assert calls["executed"]


# ── per-chat settings ───────────────────────────────────────────────────────

def test_settings_patch_validation():
    ok = session_settings.validate_patch({"approval_mode": "ask_all", "disabled_tools": ["bash", " bash", "web_fetch"],
                                          "toggles": {"mode": "agent", "web": 1, "bogus": True}})
    assert ok == {"approval_mode": "ask_all", "disabled_tools": ["bash", "web_fetch"],
                  "toggles": {"mode": "agent", "web": True}}
    assert session_settings.validate_patch({"approval_mode": None}) == {"approval_mode": None}
    for bad in ({"approval_mode": "yolo"}, {"disabled_tools": "bash"}, {"surprise": 1}, []):
        with pytest.raises(ValueError):
            session_settings.validate_patch(bad)


def test_last_used_snapshot_and_effective_mode(monkeypatch):
    snap = session_settings.last_used_from_request(chat_mode="agent", allow_web="true", allow_bash="false",
                                                   plan_mode=False, use_rag="false", workspace="/w", preset_id=None)
    assert snap == {"toggles": {"mode": "agent", "web": True, "bash": False, "plan": False, "rag": False},
                    "workspace": "/w", "preset_id": None}
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: "ask_risky" if k == "agent_approval_mode" else d)
    assert session_settings.effective_approval_mode({}) == "ask_risky"
    assert session_settings.effective_approval_mode({"approval_mode": "auto"}) == "auto"


# ── agent profiles ──────────────────────────────────────────────────────────

def test_profile_validation_normalises_and_rejects():
    out = agent_profiles.validate_profiles([
        {"name": "researcher", "model": "qwen", "disabled_tools": "bash, send_email", "max_rounds": 99},
    ])
    assert out[0]["disabled_tools"] == ["bash", "send_email"] and out[0]["max_rounds"] == agent_profiles.MAX_ROUNDS_CAP
    for bad in ([{"name": ""}], [{"name": "a"}, {"name": "A"}], "nope", [{"name": "x", "max_rounds": "lots"}]):
        with pytest.raises(ValueError):
            agent_profiles.validate_profiles(bad)
