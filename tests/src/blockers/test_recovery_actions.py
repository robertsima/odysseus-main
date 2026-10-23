"""Recovery actions on a blocker, and the document check as a blocker."""
from src import blockers, worker_preflight


def _payload(code, **extra):
    problem = {"code": code, "message": "m", **extra.pop("problem", {})}
    return {"error": "Worker not started: m", "code": code, "problems": [problem], **extra}


def test_workspace_required_offers_the_checkouts_and_running_without_files():
    b = blockers.from_preflight(_payload("WORKSPACE_REQUIRED", problem={"candidates": ["/srv/a", "/srv/b"]}))
    ids = [a["id"] for a in b.actions]
    assert ids == ["choose_workspace", "run_without_files"]
    pick = b.actions[0]
    assert [c["path"] for c in pick["candidates"]] == ["/srv/a", "/srv/b"]
    assert b.actions[1]["requires"] == ["no_workspace"]


def test_a_write_task_on_a_read_only_agent_offers_read_only_not_a_switch_to_admin():
    b = blockers.from_preflight(_payload("WRITE_NOT_ALLOWED",
                                         next_action={"enable_tools": ["write_file", "edit_file"]}))
    ids = [a["id"] for a in b.actions]
    assert "read_only" in ids and "pick_another_agent" in ids
    assert not any("admin" in (a["id"] + a["label"]).lower() for a in b.actions)


def test_allow_for_this_run_is_only_offered_for_tools_the_caller_holds():
    offered = blockers.recovery_actions("WRITE_NOT_ALLOWED", needed_tools=["write_file", "edit_file"],
                                        grantable_tools=["edit_file"])
    allow = next(a for a in offered if a["id"] == "allow_for_this_run")
    assert allow["extra_tools"] == ["edit_file"]
    none = blockers.recovery_actions("WRITE_NOT_ALLOWED", needed_tools=["write_file"], grantable_tools=[])
    assert "allow_for_this_run" not in [a["id"] for a in none]


def test_a_shell_gap_only_offers_another_agent_or_explaining():
    ids = [a["id"] for a in blockers.recovery_actions("SHELL_NOT_AVAILABLE", needed_tools=["bash"],
                                                      grantable_tools=["bash"])]
    assert ids == ["pick_another_agent", "analysis_only"]


def test_the_document_gap_becomes_document_unreadable_with_an_excerpt_action():
    gap = worker_preflight.document_gap(
        "Revise the editor document 'DA-02'",
        {"name": "Reader", "tool_access": "selected", "enabled_tools": ["read_file"], "disabled_tools": []})
    b = blockers.from_document_gap(gap)
    assert b.code == "DOCUMENT_UNREADABLE" and b.recovery == "user" and b.user_action
    assert "attach_excerpt" in [a["id"] for a in b.actions]


def test_the_public_summary_uses_the_apps_words():
    b = blockers.from_preflight(_payload("TOOLS_UNAVAILABLE", next_action={"enable_tools": ["read_file"]}))
    shown = blockers.public(b)
    assert "worker" not in shown["summary"].lower()
    assert shown["code"] == "TOOLS_UNAVAILABLE"


def test_a_stored_blocker_keeps_its_actions_through_the_round_trip():
    b = blockers.from_preflight(_payload("WORKSPACE_REQUIRED"))
    again = blockers.Blocker.from_dict(b.to_dict())
    assert again.actions == b.actions and again == b
