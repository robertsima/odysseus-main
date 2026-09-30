"""A loadout's tool allowlist keeps the tools that touch only the agent's own state.

The Lead Engineer loadout lists its tools ("selected"), written before the
task checklist and the recall tools existed, so on 2026-09-29 it had no
update_plan (the checklist that carries a request through) and no
recall_tool_output, though every offloaded excerpt told it to call that.
"""
from pathlib import Path

from src.tool_policy import SELF_SCOPED_TOOLS, denied_by_allowlist

LEAD_ENGINEER = ["apply_patch", "ask_user", "bash", "discover_tools", "edit_file", "grep", "read_file"]
CANDIDATES = ["bash", "web_search", "update_plan", "recall_tool_output", "recall_chat_history",
              "discover_tools", "manage_calendar"]


def test_selected_allowlist_keeps_self_scoped_tools():
    denied = denied_by_allowlist(CANDIDATES, tool_access="selected", enabled_tools=LEAD_ENGINEER)
    assert denied == {"web_search", "manage_calendar"}
    assert SELF_SCOPED_TOOLS == {"discover_tools", "recall_tool_output", "recall_chat_history", "update_plan"}


def test_an_explicit_denial_still_wins():
    denied = denied_by_allowlist(CANDIDATES, tool_access="selected", enabled_tools=LEAD_ENGINEER,
                                 disabled_tools=["update_plan"])
    assert "update_plan" in denied and "recall_chat_history" not in denied


def test_no_tools_and_empty_allowlists_grant_nothing():
    assert set(CANDIDATES) <= denied_by_allowlist(CANDIDATES, tool_access="none", enabled_tools=LEAD_ENGINEER)
    assert set(CANDIDATES) <= denied_by_allowlist(CANDIDATES, tool_access="selected", enabled_tools=[])


def test_execution_admits_what_the_offer_admits():
    src = (Path(__file__).resolve().parents[1] / "src" / "tool_execution.py").read_text(encoding="utf-8")
    assert "tool in _SELF_SCOPED_TOOLS and _tool_access == \"selected\" and bool(_enabled)" in src
