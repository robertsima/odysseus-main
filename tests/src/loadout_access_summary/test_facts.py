"""The sentences the Talk and Assign screens show about an agent.

The production bug: the summary said what a person could not rely on (an agent
that "can run commands" with its shell switched off, or one that "can contact
other agents" when a worker never can). Each case below builds the profile the
way a loadout is stored and checks the facts the formatter derives from its
tools.
"""
import pytest

from src import agent_profiles, loadout_access_summary as summary

EVERY_TOOL = {"read_file", "grep", "ls", "glob", "write_file", "edit_file", "apply_patch", "bash", "python",
              "search_documents", "manage_notes", "manage_documents", "web_search", "send_email",
              "delegate_to_agent", "orchestrate_agents", "manage_agent_loadout", "send_to_session"}


def _profile(**raw):
    return agent_profiles.validate_profiles([{"name": "Agent", **raw}])[0]


def _levels(mode):
    return {fact["level"]: fact["text"] for fact in mode["facts"]}


def test_read_only_loadout_reads_files_and_cannot_change_them():
    profile = _profile(tool_access="selected", enabled_tools=["read_file", "grep", "search_documents"],
                       shell_access="off")
    out = summary.build_access(profile, talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    texts = _levels(out["talk"])
    assert texts["can"] == "Can read project files and search indexed notes."
    assert "change files" in texts["cannot"] and "run commands" in texts["cannot"]
    assert "asks" not in texts


def test_write_and_shell_loadout_says_it_asks_before_external_actions():
    profile = _profile(tool_access="selected", delegation_policy="never",
                       enabled_tools=["read_file", "edit_file", "bash", "send_email"], approval_mode="ask_risky")
    out = summary.build_access(profile, talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    texts = _levels(out["talk"])
    assert texts["can"] == "Can read and edit project files and run commands."
    assert texts["cannot"] == "Cannot contact other agents."
    assert texts["asks"] == "Asks before external actions."
    assert out["talk"]["summary"] == "Can read and edit project files and run commands. Cannot contact other agents. Asks before external actions."


def test_assign_summary_says_results_return_and_main_view_has_at_most_three_facts():
    out = summary.build_access(_profile(tool_access="selected", enabled_tools=["read_file", "web_search"]),
                               talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    assert out["assign"]["summary"].endswith("Results return to this chat.")
    assert "Results return" not in out["talk"]["summary"]
    assert len(out["talk"]["facts"]) <= 3 and len(out["assign"]["facts"]) <= 3


def test_a_loadout_with_no_tools_only_answers():
    out = summary.build_access(_profile(tool_access="none"), talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    texts = _levels(out["talk"])
    assert texts["can"] == "Can answer from this conversation."
    assert "run commands" in texts["cannot"] and "open files or notes" in texts["cannot"]
    assert out["advanced"]["tool_access"] == "No tools"


def test_shell_switched_off_removes_the_command_claim_even_with_the_tool_granted():
    profile = _profile(tool_access="selected", enabled_tools=["bash", "read_file"], shell_access="off")
    out = summary.build_access(profile, talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    assert "run commands" not in _levels(out["talk"])["can"]


def test_a_task_does_not_claim_the_agent_can_start_other_agents():
    profile = _profile(tool_access="selected", enabled_tools=["read_file", "delegate_to_agent"],
                       delegation_policy="never")
    out = summary.build_access(profile, talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    assert "contact other agents" not in _levels(out["assign"])["can"]


def test_the_assigned_agent_holds_no_more_than_the_chat_that_starts_it():
    profile = _profile(tool_access="all")
    starter = EVERY_TOOL - {"bash", "python", "write_file", "edit_file", "apply_patch"}
    out = summary.build_access(profile, talk_allowed=EVERY_TOOL, starter_allowed=starter)
    assert "run commands" in _levels(out["talk"])["can"]
    assert "run commands" not in _levels(out["assign"])["can"]


@pytest.mark.parametrize("approval,level", [("auto", "can"), ("ask_risky", "asks")])
def test_external_actions_without_asking_are_a_capability_not_a_question(approval, level):
    profile = _profile(tool_access="selected", enabled_tools=["send_email"], approval_mode=approval)
    out = summary.build_access(profile, talk_allowed=EVERY_TOOL, starter_allowed=EVERY_TOOL)
    texts = _levels(out["talk"])
    if level == "can":
        assert "without asking" in texts["can"] and "asks" not in texts
    else:
        assert texts["asks"] == "Asks before external actions."
