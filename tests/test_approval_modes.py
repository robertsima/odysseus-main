"""Approval modes decide which tool calls stop for an approval card."""

from collections import namedtuple

import pytest

from src import approval_modes
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action


ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


def _ctx(mode, *, tainted=False, bypassed=False):
    return ToolRunSecurityContext(
        external_untrusted_context_seen=tainted,
        approval_gate_bypassed=bypassed,
        approval_mode=mode,
    )


def test_no_mode_keeps_upstreams_untrusted_context_gate():
    assert _ctx(None).decision_for("bash", "ls").allowed
    assert not _ctx(None, tainted=True).decision_for("bash", "ls").allowed


def test_auto_never_asks_even_after_untrusted_context():
    # The sub-agent case: get_workspace/read_file arm the untrusted-context
    # flag, and under auto the next bash call still runs.
    ctx = _ctx("auto", tainted=True)
    for tool, content in (
        ("bash", "ls -la"),
        ("bash", "rm -rf build"),
        ("write_file", '{"path": "a.txt", "content": "x"}'),
        ("send_email", "{}"),
        ("mcp__firecrawl__scrape", "{}"),
    ):
        assert ctx.decision_for(tool, content).allowed, tool


@pytest.mark.parametrize("tainted", [False, True])
def test_ask_risky_asks_only_for_destructive_or_outward_calls(tainted):
    ctx = _ctx("ask_risky", tainted=tainted)
    assert ctx.decision_for("bash", "ls -la && pytest -q").allowed
    assert ctx.decision_for("read_file", '{"path": "a"}').allowed
    assert ctx.decision_for("write_file", '{"path": "a", "content": "x"}').allowed
    assert ctx.decision_for("list_emails", "{}").allowed

    for tool, content in (
        ("bash", "rm -rf build"),
        ("bash", "git push origin dev"),
        ("send_email", "{}"),
        ("mcp__email__delete_email", "{}"),
        ("manage_settings", "{}"),
        ("manage_notes", '{"action": "delete", "id": 3}'),
        ("manage_git", '{"action": "push"}'),
    ):
        decision = ctx.decision_for(tool, content)
        assert not decision.allowed, tool
        assert "Ask for risky actions" in decision.reason


def test_ask_risky_does_not_ask_for_git_reads():
    ctx = _ctx("ask_risky")
    assert ctx.decision_for("manage_git", '{"action": "status"}').allowed


def test_ask_all_asks_for_every_change_but_not_reads():
    ctx = _ctx("ask_all")
    assert ctx.decision_for("read_file", '{"path": "a"}').allowed
    assert ctx.decision_for("grep", '{"pattern": "x"}').allowed
    assert ctx.decision_for("todowrite", "[]").allowed
    for tool in ("bash", "python", "write_file", "manage_notes", "mcp__firecrawl__scrape"):
        assert not ctx.decision_for(tool, "{}").allowed, tool


def test_ask_all_keeps_the_untrusted_context_gate_for_private_reads():
    assert _ctx("ask_all").decision_for("list_emails", "{}").allowed
    assert not _ctx("ask_all", tainted=True).decision_for("list_emails", "{}").allowed


def test_a_task_or_chat_grant_lifts_the_mode_gate():
    assert _ctx("ask_all", bypassed=True).decision_for("bash", "rm -rf x").allowed


def test_delegated_credential_block_still_wins_over_auto():
    ctx = ToolRunSecurityContext(delegated_credential=True, approval_mode="auto")
    assert not ctx.decision_for("bash", "ls").allowed


def test_mode_reason_names_the_setting():
    reason = approval_modes.mode_reason("ask_risky", "bash", "sudo reboot")
    assert "Ask for risky actions" in reason and "bash" in reason
    assert approval_modes.mode_reason("auto", "bash", "sudo reboot") is None
    assert approval_modes.mode_reason("bogus", "bash", "sudo reboot") is None


def test_pending_keeps_the_reason_for_the_agents_panel():
    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="bash",
        content="rm -rf x", workspace=None, external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("bash", "rm -rf x"), reason="it deletes",
    )
    assert pending.reason == "it deletes"
    assert pending.public_payload()["description"] == "it deletes"


@pytest.mark.asyncio
async def test_mode_approval_runs_without_untrusted_context(monkeypatch):
    """An approval raised by the mode carries no taint and must still execute."""
    import src.tool_execution as tool_execution

    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="bash",
        content="rm -rf build", workspace=None, external_untrusted_context_seen=False,
        capabilities=capabilities_for_action("bash", "rm -rf build"),
    )
    grant = store.consume(pending.approval_id, decision="approve_task", owner="alice", session_id="s1")

    async def fake_implementation(block, **kwargs):
        return "bash", {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_implementation)
    _, result = await tool_execution.execute_tool_block(
        ToolBlock("bash", "rm -rf build"),
        session_id="s1", owner="alice", workspace=None,
        security_context=_ctx("ask_risky", bypassed=True),
        exact_approval=grant,
    )
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_tainted_approval_still_needs_an_armed_context(monkeypatch):
    import src.tool_execution as tool_execution

    store = ToolApprovalStore()
    pending = store.create(
        owner="alice", session_id="s1", origin_run_id="r1", tool_name="bash",
        content="printf x", workspace=None, external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf x"),
    )
    grant = store.consume(pending.approval_id, decision="approve_task", owner="alice", session_id="s1")
    _, result = await tool_execution.execute_tool_block(
        ToolBlock("bash", "printf x"),
        session_id="s1", owner="alice", workspace=None,
        security_context=_ctx(None),
        exact_approval=grant,
    )
    assert result.get("blocked") is True


# ── Integrations and calendar under ask_risky (2026-09-22 audit) ────────

def test_ask_risky_asks_for_integration_writes_but_not_reads(monkeypatch):
    import src.mcp_manager as mcp

    lotus = {
        "mcp__lotus__mood_summarize_period": {"name": "mood_summarize_period"},
        "mcp__lotus__mood_detect_low_energy_patterns": {"name": "mood_detect_low_energy_patterns"},
        "mcp__lotus__log_mood": {"name": "log_mood"},
    }
    monkeypatch.setattr(mcp, "mcp_tool_metadata", lambda name: lotus.get(name))
    ctx = _ctx("ask_risky")
    for args in (["today", "--json"], ["upcoming", "7"], ["task", "list", "--json"], ["project", "list"]):
        assert ctx.decision_for("mcp__todoist__todoist", {"args": args}).allowed, args
    assert ctx.decision_for("mcp__lotus__mood_summarize_period", "{}").allowed
    assert ctx.decision_for("mcp__lotus__mood_detect_low_energy_patterns", "{}").allowed

    for tool, content in (
        ("mcp__todoist__todoist", {"args": ["task", "quickadd", "Finish report tomorrow p1"]}),
        ("mcp__todoist__todoist", {"args": ["complete", "123"]}),
        ("mcp__todoist__todoist", "not json"),
        ("mcp__lotus__log_mood", "{}"),
        ("mcp__unknown__anything", "{}"),
    ):
        decision = ctx.decision_for(tool, content)
        assert not decision.allowed, (tool, content)
        assert "integration" in decision.reason


def test_ask_risky_asks_before_calendar_creates_and_edits():
    ctx = _ctx("ask_risky")
    assert ctx.decision_for("manage_calendar", '{"action": "list_events"}').allowed
    for action in ("create_event", "create", "update_event", "delete_event"):
        assert not ctx.decision_for("manage_calendar", '{"action": "%s"}' % action).allowed, action


def test_ask_all_lets_integration_reads_run(monkeypatch):
    import src.mcp_manager as mcp

    monkeypatch.setattr(mcp, "mcp_tool_metadata", lambda name: {"name": name.rsplit("__", 1)[-1]})
    ctx = _ctx("ask_all")
    assert ctx.decision_for("mcp__todoist__todoist", {"args": ["today"]}).allowed
    assert ctx.decision_for("mcp__lotus__mood_summarize_period", "{}").allowed
    assert not ctx.decision_for("mcp__todoist__todoist", {"args": ["task", "add", "x"]}).allowed


def test_todoist_read_classifier_fails_closed():
    from src.mcp_manager import todoist_args_readonly

    assert todoist_args_readonly(["today", "--json"])
    assert todoist_args_readonly(["--help"])
    assert not todoist_args_readonly(["task", "quickadd", "x"])
    assert not todoist_args_readonly("today")
    assert not todoist_args_readonly(None)


def test_read_only_worker_may_list_todoist_but_not_write():
    from src.mcp_manager import mcp_call_is_readonly

    assert mcp_call_is_readonly("mcp__todoist__todoist", '{"args": ["today", "--json"]}')
    assert not mcp_call_is_readonly("mcp__todoist__todoist", '{"args": ["task", "add", "x"]}')
    # Unknown tool with no catalogue entry fails closed.
    assert not mcp_call_is_readonly("mcp__x__y", "{}", None)


def test_noun_first_names_read_only_when_no_write_verb():
    from src.mcp_manager import mcp_tool_is_readonly

    assert mcp_tool_is_readonly({"name": "mood_summarize_period"})
    assert mcp_tool_is_readonly({"name": "mood_detect_low_energy_patterns"})
    assert not mcp_tool_is_readonly({"name": "mood_log_entry"})
    assert not mcp_tool_is_readonly({"name": "task_delete_list"})
    assert not mcp_tool_is_readonly({"name": "mood_entries"})
