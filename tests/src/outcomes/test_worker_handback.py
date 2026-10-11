"""record_worker_handback: what a worker's final report does to the outcome it serves."""
import pytest

DONE_WITH_TESTS = "Outcome: done\nChanged: src/parser.py\nChecked: pytest tests/src/parser -q: 129 passed\n"
DONE_NO_FACTS = "Outcome: done\nChanged: updated the parser.\nChecked: looked it over, it works.\n"
PARTIAL = "**Outcome: partly done.**\nChanged: src/a.py\nOpen:\n- wire the retry path\n- update the docs\n"
BLOCKED_NEEDS_USER = "Outcome: blocked\nOpen: deploy\nNeeds user: paste the staging deploy key into note 'deploy'\n"


def _linked(store, owner="alice", **links):
    out = store.create(owner, "Ship the parser")["outcome"]
    for kind, ref in links.items():
        store.add_link(owner, out["id"], {"chat": "chat", "run": "agent_run"}[kind], ref, actor="person")
    store.set_state(owner, out["id"], "active", actor="person")
    return out["id"]


def _record(store, oid, text, *, summary=None, owner="alice", session="w1", run="run-1", status="completed"):
    return store.record_worker_handback(owner, session, run, text, status, summary or {})


def test_done_with_evidence_marks_the_outcome_done_and_attaches_the_evidence(outcomes):
    oid = _linked(outcomes, chat="w1")

    result = _record(outcomes, oid, DONE_WITH_TESTS, summary={"commit": "9f8e7d6aaa"})

    got = outcomes.get("alice", oid)
    assert got["state"] == "done"
    assert {e["kind"] for e in got["evidence"]} == {"commit", "tests"}
    assert all(e["by"] == "agent:w1" for e in got["evidence"])
    assert result["outcomes"][0]["id"] == oid


def test_done_without_evidence_stays_active_and_asks_for_confirmation(outcomes):
    """Production bug: a worker said done with nothing behind it and the system called it complete."""
    oid = _linked(outcomes, chat="w1")

    _record(outcomes, oid, DONE_NO_FACTS)

    got = outcomes.get("alice", oid)
    assert got["state"] == "active"
    assert got["next_action"] == "Confirm the result or ask for evidence"
    assert got["evidence"] == [] and got["completed_at"] is None


def test_partial_stays_active_and_takes_its_next_action_from_the_first_open_item(outcomes):
    oid = _linked(outcomes, chat="w1")

    _record(outcomes, oid, PARTIAL)

    got = outcomes.get("alice", oid)
    assert got["state"] == "active" and got["next_action"] == "wire the retry path"


def test_a_needs_user_line_blocks_the_outcome_on_the_person(outcomes):
    oid = _linked(outcomes, chat="w1")

    _record(outcomes, oid, BLOCKED_NEEDS_USER)

    got = outcomes.get("alice", oid)
    assert got["state"] == "blocked"
    assert got["blocker"]["code"] == "NEEDS_USER" and got["blocker"]["recovery"] == "user"
    assert got["blocker"]["user_action"].startswith("paste the staging deploy key")
    assert got["blocker"]["source"] == {"kind": "chat", "ref": "w1"}


def test_a_needs_user_line_blocks_even_when_the_worker_says_partly_done(outcomes):
    oid = _linked(outcomes, chat="w1")

    _record(outcomes, oid, "Outcome: partly done\nOpen: the rest\nNeeds user: pick a region\n")

    assert outcomes.get("alice", oid)["state"] == "blocked"


def test_blocked_without_a_needs_line_still_blocks_with_an_action_for_the_person(outcomes):
    oid = _linked(outcomes, chat="w1")

    _record(outcomes, oid, "Outcome: blocked, the CI never went green.\n")

    got = outcomes.get("alice", oid)
    assert got["state"] == "blocked"
    assert got["blocker"]["code"] == "WORKER_BLOCKED" and got["blocker"]["user_action"]


def test_a_later_handback_that_reports_progress_clears_the_blocker(outcomes):
    oid = _linked(outcomes, chat="w1")
    _record(outcomes, oid, BLOCKED_NEEDS_USER)

    _record(outcomes, oid, PARTIAL)

    got = outcomes.get("alice", oid)
    assert got["state"] == "active" and got["blocker"] is None


def test_the_same_handback_twice_does_not_duplicate_evidence(outcomes):
    oid = _linked(outcomes, chat="w1")
    summary = {"commit": "9f8e7d6aaa"}
    _record(outcomes, oid, "Outcome: partly done\nOpen: docs\n", summary=summary)
    _record(outcomes, oid, "Outcome: partly done\nOpen: docs\n", summary=summary)

    assert len(outcomes.get("alice", oid)["evidence"]) == 1


def test_the_outcome_is_found_through_the_run_or_the_parent_chat(outcomes):
    by_run = _linked(outcomes, run="run-9")
    by_parent = _linked(outcomes, chat="parent-1")

    result = _record(outcomes, by_run, PARTIAL, session="w2", run="run-9",
                     summary={"parent_session": "parent-1"})

    assert {o["id"] for o in result["outcomes"]} == {by_run, by_parent}
    assert outcomes.get("alice", by_parent)["next_action"] == "wire the retry path"


def test_a_finished_outcome_is_left_alone(outcomes):
    oid = _linked(outcomes, chat="w1")
    outcomes.set_state("alice", oid, "dropped", actor="person")

    result = _record(outcomes, oid, PARTIAL)

    assert result["outcomes"] == []
    assert outcomes.get("alice", oid)["state"] == "dropped"


def test_a_waiting_outcome_keeps_its_state_but_takes_the_evidence(outcomes):
    oid = _linked(outcomes, chat="w1")
    outcomes.set_state("alice", oid, "waiting", actor="person", waiting_on="Dana")

    _record(outcomes, oid, "Outcome: partly done\nOpen: x\n", summary={"commit": "abc1234"})

    got = outcomes.get("alice", oid)
    assert got["state"] == "waiting" and len(got["evidence"]) == 1


def test_a_handback_without_an_outcome_line_changes_no_state(outcomes):
    oid = _linked(outcomes, chat="w1")

    _record(outcomes, oid, "All sorted, nothing more to say.")

    assert outcomes.get("alice", oid)["state"] == "active"


def test_the_hook_never_raises(outcomes, monkeypatch):
    """It runs inside agent_control's hand-back path; a store failure must not break delivery."""
    monkeypatch.setattr(outcomes, "find_by_link", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))

    result = outcomes.record_worker_handback("alice", "w1", "run-1", DONE_WITH_TESTS, "completed", {})

    assert result["outcomes"] == [] and "db down" in result["error"]
    assert outcomes.record_worker_handback("alice", "w1", "run-1", None, None, None)["outcomes"] == []


@pytest.mark.security
def test_a_handback_cannot_touch_another_owners_outcome_linked_to_the_same_chat(outcomes):
    bob_oid = _linked(outcomes, owner="bob", chat="shared-id")

    result = _record(outcomes, bob_oid, DONE_WITH_TESTS, owner="alice", session="shared-id",
                     summary={"commit": "abc1234"})

    assert result["outcomes"] == []
    got = outcomes.get("bob", bob_oid)
    assert got["state"] == "active" and got["evidence"] == []
