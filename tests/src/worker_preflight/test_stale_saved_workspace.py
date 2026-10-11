"""A saved workspace that stopped working is named, not reported as missing.

Bug (2026-10-10): a resume failed with "this chat has none" while the chat still
held a workspace. Preflight had dropped the saved path without a word because it
no longer passed `vet_workspace`, so the person could not tell what to repair.
"""
import pytest

from src import worker_preflight as wp


@pytest.fixture(autouse=True)
def admin_owner(monkeypatch):
    import src.tool_security as tool_security
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(tool_security, "owner_baseline_disabled_tools", lambda owner: set())


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    paths = []
    for name in ("odysseus-main", "other-repo"):
        repo = tmp_path / "dev" / name
        (repo / ".git").mkdir(parents=True)
        paths.append(str(repo.resolve()))
    monkeypatch.setattr(wp, "known_checkouts", lambda: list(paths))
    return paths


TASK = "Fix the bug in src/app.py"


def test_a_deleted_saved_workspace_is_named_with_the_reason(checkouts, tmp_path):
    gone = str(tmp_path / "agent_worktrees" / "consolidation-outcomes-blockers")

    payload = wp.run_preflight(TASK, inherited_workspace=gone).blocked_payload()

    assert payload["status"] == "blocked"
    assert gone in payload["error"]
    assert "does not exist" in payload["error"]
    assert "this chat has none" not in payload["error"]
    assert payload["problems"][0]["candidates"] == checkouts
    assert payload["next_action"]["retry_with"] == {"workspace": checkouts[0]}


def test_a_saved_file_path_says_it_is_not_a_folder(checkouts, tmp_path):
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")

    error = wp.run_preflight(TASK, inherited_workspace=str(a_file)).blocked_payload()["error"]

    assert str(a_file) in error and "not a folder" in error


def test_a_saved_path_in_application_state_says_so(checkouts, tmp_path, monkeypatch):
    from src import constants

    data = tmp_path / "data"
    (data / "secrets").mkdir(parents=True)
    monkeypatch.setattr(constants, "DATA_DIR", str(data))

    error = wp.run_preflight(TASK, inherited_workspace=str(data / "secrets")).blocked_payload()["error"]

    assert "application data" in error


def test_a_stale_saved_path_does_not_block_a_task_that_needs_no_repository(checkouts, tmp_path):
    pf = wp.run_preflight("Summarise today's AI news", inherited_workspace=str(tmp_path / "gone"))

    assert pf.ok
    assert any("saved workspace" in w and "gone" in w for w in pf.warnings)


def test_the_only_checkout_replaces_a_stale_saved_path_and_the_warning_says_so(tmp_path, monkeypatch):
    only = tmp_path / "dev" / "only"
    (only / ".git").mkdir(parents=True)
    monkeypatch.setattr(wp, "known_checkouts", lambda: [str(only.resolve())])

    pf = wp.run_preflight(TASK, inherited_workspace=str(tmp_path / "gone"))

    assert pf.ok and pf.workspace == str(only.resolve())
    assert any("saved workspace" in w for w in pf.warnings)


def test_an_invalid_explicit_workspace_lists_only_vetted_candidates(checkouts, tmp_path, monkeypatch):
    unusable = tmp_path / "listed-but-refused"
    unusable.mkdir()
    monkeypatch.setattr(wp, "known_checkouts", lambda: [checkouts[0], str(unusable)])
    monkeypatch.setattr("src.tool_execution.vet_workspace",
                        lambda raw: None if "listed-but-refused" in str(raw) or "missing" in str(raw) else str(raw))

    payload = wp.run_preflight(TASK, explicit_workspace=str(tmp_path / "missing")).blocked_payload()

    assert payload["problems"][0]["candidates"] == [checkouts[0]]
