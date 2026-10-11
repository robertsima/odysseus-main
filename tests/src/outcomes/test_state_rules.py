"""The outcome store's state rules and its append-only history."""
import pytest

from src.blockers import Blocker


def _make(store, owner="alice", **kw):
    res = store.create(owner, kw.pop("title", "Ship the importer"), **kw)
    assert res["ok"], res
    return res["outcome"]


def _kinds(store, owner, oid):
    return [e["kind"] for e in store.history(owner, oid)]


def test_create_trims_the_title_and_records_a_created_event(outcomes):
    out = _make(outcomes, title="  Renew passport  ")

    assert out["title"] == "Renew passport"
    assert out["state"] == "open" and out["kind"] == "obligation"
    assert _kinds(outcomes, "alice", out["id"]) == ["created"]


def test_create_refuses_an_empty_title(outcomes):
    res = outcomes.create("alice", "   ")

    assert res["ok"] is False and res["code"] == "invalid"


def test_done_is_refused_without_evidence_or_confirmation(outcomes):
    """Production bug: a worker turn ended and the outcome was marked complete."""
    out = _make(outcomes)

    res = outcomes.set_state("alice", out["id"], "done", actor="agent:w1")

    assert res["ok"] is False and res["code"] == "needs_evidence"
    assert "evidence" in res["error"] and "confirm" in res["error"]
    assert outcomes.get("alice", out["id"])["state"] == "open"
    assert outcomes.get("alice", out["id"])["completed_at"] is None


def test_done_is_accepted_once_evidence_is_attached(outcomes):
    out = _make(outcomes)
    outcomes.add_evidence("alice", out["id"], {"kind": "commit", "ref": "abc123", "detail": "fix"}, actor="agent:w1")

    res = outcomes.set_state("alice", out["id"], "done", actor="agent:w1")

    assert res["ok"] is True
    got = outcomes.get("alice", out["id"])
    assert got["state"] == "done" and got["completed_at"] is not None
    assert got["confirmed_by_person"] is False


def test_the_person_can_confirm_done_without_evidence(outcomes):
    out = _make(outcomes)

    res = outcomes.set_state("alice", out["id"], "done", actor="person", confirmed_by_person=True)

    assert res["ok"] is True
    assert outcomes.get("alice", out["id"])["confirmed_by_person"] is True


def test_an_agent_cannot_confirm_for_the_person(outcomes):
    """Only the person's own actor can set confirmed_by_person, whatever the caller passes."""
    out = _make(outcomes)

    res = outcomes.set_state("alice", out["id"], "done", actor="agent:w1", confirmed_by_person=True)

    assert res["ok"] is False and res["code"] == "needs_evidence"
    assert outcomes.get("alice", out["id"])["confirmed_by_person"] is False


def test_an_agent_cannot_pose_as_the_person_in_evidence(outcomes):
    out = _make(outcomes)

    outcomes.add_evidence("alice", out["id"],
                          {"kind": "note", "ref": "n1", "detail": "x", "by": "person"}, actor="agent:w1")

    assert outcomes.get("alice", out["id"])["evidence"][0]["by"] == "agent:w1"


def test_evidence_with_an_unknown_kind_is_refused(outcomes):
    out = _make(outcomes)

    res = outcomes.add_evidence("alice", out["id"], {"kind": "vibes", "ref": "x"}, actor="agent:w1")

    assert res["ok"] is False and res["code"] == "invalid"


def test_reopening_a_done_outcome_drops_the_old_confirmation(outcomes):
    """Otherwise a later 'done' would pass on a confirmation the person gave for the first attempt."""
    out = _make(outcomes)
    outcomes.set_state("alice", out["id"], "done", actor="person", confirmed_by_person=True)
    outcomes.set_state("alice", out["id"], "active", actor="person")

    res = outcomes.set_state("alice", out["id"], "done", actor="agent:w1")

    assert res["ok"] is False
    assert outcomes.get("alice", out["id"])["completed_at"] is None


def test_setting_a_blocker_moves_active_to_blocked_and_clearing_moves_back(outcomes):
    out = _make(outcomes)
    outcomes.set_state("alice", out["id"], "active", actor="person")
    blocker = Blocker(code="NEEDS_USER", summary="needs a key", recovery="user", user_action="Paste the key")

    outcomes.set_blocker("alice", out["id"], blocker, actor="agent:w1")
    blocked = outcomes.get("alice", out["id"])
    outcomes.set_blocker("alice", out["id"], None, actor="person")
    cleared = outcomes.get("alice", out["id"])

    assert blocked["state"] == "blocked" and blocked["blocker"]["code"] == "NEEDS_USER"
    assert cleared["state"] == "active" and cleared["blocker"] is None


def test_a_blocker_on_an_open_outcome_blocks_it(outcomes):
    out = _make(outcomes)

    outcomes.set_blocker("alice", out["id"],
                         {"code": "X", "summary": "s", "recovery": "retry"}, actor="system")

    assert outcomes.get("alice", out["id"])["state"] == "blocked"


def test_a_blocker_on_a_waiting_outcome_keeps_it_waiting(outcomes):
    out = _make(outcomes)
    outcomes.set_state("alice", out["id"], "waiting", actor="person", waiting_on="Dana")

    outcomes.set_blocker("alice", out["id"], {"code": "X", "summary": "s", "recovery": "retry"}, actor="system")

    assert outcomes.get("alice", out["id"])["state"] == "waiting"


def test_blocked_cannot_be_set_directly_without_a_blocker(outcomes):
    out = _make(outcomes)

    res = outcomes.set_state("alice", out["id"], "blocked", actor="person")

    assert res["ok"] is False and res["code"] == "invalid"


def test_waiting_without_waiting_on_is_refused(outcomes):
    out = _make(outcomes)

    res = outcomes.set_state("alice", out["id"], "waiting", actor="person")

    assert res["ok"] is False and res["code"] == "needs_waiting_on"
    assert outcomes.get("alice", out["id"])["state"] == "open"


def test_waiting_with_waiting_on_and_follow_up_is_stored(outcomes):
    out = _make(outcomes)

    res = outcomes.set_state("alice", out["id"], "waiting", actor="person",
                             waiting_on="Dana", follow_up_at="2026-10-14T09:00:00")

    assert res["ok"] is True
    got = outcomes.get("alice", out["id"])
    assert got["waiting_on"] == "Dana" and got["follow_up_at"].startswith("2026-10-14T09:00")


def test_leaving_waiting_clears_waiting_on(outcomes):
    out = _make(outcomes)
    outcomes.set_state("alice", out["id"], "waiting", actor="person", waiting_on="Dana")

    outcomes.set_state("alice", out["id"], "active", actor="person")

    got = outcomes.get("alice", out["id"])
    assert got["waiting_on"] is None and got["follow_up_at"] is None


def test_dropped_sets_completed_at_and_clears_the_blocker(outcomes):
    out = _make(outcomes)
    outcomes.set_blocker("alice", out["id"], {"code": "X", "summary": "s", "recovery": "retry"}, actor="system")

    outcomes.set_state("alice", out["id"], "dropped", actor="person")

    got = outcomes.get("alice", out["id"])
    assert got["state"] == "dropped" and got["completed_at"] and got["blocker"] is None


def test_an_unknown_state_is_refused(outcomes):
    out = _make(outcomes)

    assert outcomes.set_state("alice", out["id"], "finished", actor="person")["code"] == "invalid"


def test_every_mutation_writes_an_event(outcomes):
    out = _make(outcomes)
    oid = out["id"]

    outcomes.update("alice", oid, {"next_action": "Call the bank"}, actor="person")
    outcomes.add_link("alice", oid, "chat", "s1", actor="person")
    outcomes.add_evidence("alice", oid, {"kind": "note", "ref": "n1", "detail": "x"}, actor="person")
    outcomes.set_blocker("alice", oid, {"code": "X", "summary": "s", "recovery": "retry"}, actor="system")
    outcomes.set_blocker("alice", oid, None, actor="system")
    outcomes.set_state("alice", oid, "done", actor="person", confirmed_by_person=True)

    kinds = _kinds(outcomes, "alice", oid)
    assert kinds[0] == "created"
    assert kinds.count("edit") == 1 and kinds.count("link") == 1 and kinds.count("evidence") == 1
    assert kinds.count("blocker") == 2
    assert kinds.count("state") >= 2          # blocked, active, done
    actors = {e["actor"] for e in outcomes.history("alice", oid)}
    assert actors == {"person", "system"}


def test_a_refused_mutation_writes_no_event(outcomes):
    out = _make(outcomes)

    outcomes.set_state("alice", out["id"], "done", actor="agent:w1")

    assert _kinds(outcomes, "alice", out["id"]) == ["created"]


def test_update_changes_only_editable_fields(outcomes):
    out = _make(outcomes)

    res = outcomes.update("alice", out["id"],
                          {"state": "done", "confirmed_by_person": True, "evidence_json": "[1]", "owner": "bob",
                           "title": "New title"}, actor="person")

    assert res["ok"] is True
    got = outcomes.get("alice", out["id"])
    assert got["title"] == "New title"
    assert got["state"] == "open" and got["confirmed_by_person"] is False and got["evidence"] == []
    assert res["ignored"] == ["confirmed_by_person", "evidence_json", "owner", "state"]


def test_an_agent_cannot_set_priority_or_replace_a_due_date(outcomes):
    """Nothing silent: priority is the person's, and a set due date moves only on their say-so."""
    out = _make(outcomes, due_at="2026-10-20T00:00:00", priority=2)

    res = outcomes.update("alice", out["id"], {"priority": 1, "due_at": "2026-11-01T00:00:00"}, actor="agent:w1")

    assert res["ok"] is False and res["code"] == "forbidden"
    got = outcomes.get("alice", out["id"])
    assert got["priority"] == 2 and got["due_at"].startswith("2026-10-20")


def test_an_agent_may_set_a_due_date_when_none_exists(outcomes):
    out = _make(outcomes)

    res = outcomes.update("alice", out["id"], {"due_at": "2026-11-01T00:00:00"}, actor="agent:w1")

    assert res["ok"] is True


def test_priority_must_be_between_1_and_4(outcomes):
    out = _make(outcomes)

    assert outcomes.update("alice", out["id"], {"priority": 9}, actor="person")["code"] == "invalid"


def test_links_are_idempotent_and_found_by_target(outcomes):
    out = _make(outcomes)
    first = outcomes.add_link("alice", out["id"], "todoist_task", "8431", label="Pay rent", actor="person")
    again = outcomes.add_link("alice", out["id"], "todoist_task", "8431", actor="person")

    assert first["ok"] and again["ok"] and again["link"]["id"] == first["link"]["id"]
    assert len(outcomes.get("alice", out["id"])["links"]) == 1
    assert [o["id"] for o in outcomes.find_by_link("alice", "todoist_task", "8431")] == [out["id"]]


def test_unknown_link_kind_is_refused(outcomes):
    out = _make(outcomes)

    assert outcomes.add_link("alice", out["id"], "email", "x", actor="person")["code"] == "invalid"


def test_remove_link_detaches_it(outcomes):
    out = _make(outcomes)
    link = outcomes.add_link("alice", out["id"], "chat", "s1", actor="person")["link"]

    res = outcomes.remove_link("alice", out["id"], link["id"], actor="person")

    assert res["ok"] and outcomes.get("alice", out["id"])["links"] == []
    assert outcomes.find_by_link("alice", "chat", "s1") == []


def test_list_filters_by_state_and_newest_touched_first(outcomes):
    a = _make(outcomes, title="a")
    b = _make(outcomes, title="b")
    outcomes.set_state("alice", a["id"], "active", actor="person")

    assert [o["id"] for o in outcomes.list_for("alice", states=["active"])] == [a["id"]]
    assert {o["id"] for o in outcomes.list_for("alice")} == {a["id"], b["id"]}


@pytest.mark.security
def test_one_owner_cannot_read_or_change_another_owners_outcome(outcomes):
    """Owner scope: every read and write filters on owner, with no fallback to the id alone."""
    bob_out = _make(outcomes, owner="bob", title="Bob's private thing")
    oid = bob_out["id"]
    outcomes.add_link("bob", oid, "chat", "bob-chat", actor="person")

    assert outcomes.get("alice", oid) is None
    assert outcomes.list_for("alice") == []
    assert outcomes.history("alice", oid) == []
    assert outcomes.find_by_link("alice", "chat", "bob-chat") == []
    for result in (
        outcomes.update("alice", oid, {"title": "mine now"}, actor="person"),
        outcomes.set_state("alice", oid, "dropped", actor="person"),
        outcomes.set_blocker("alice", oid, None, actor="person"),
        outcomes.add_link("alice", oid, "chat", "x", actor="person"),
        outcomes.add_evidence("alice", oid, {"kind": "note", "ref": "n"}, actor="person"),
    ):
        assert result["ok"] is False and result["code"] == "not_found"
    link_id = outcomes.get("bob", oid)["links"][0]["id"]
    assert outcomes.remove_link("alice", oid, link_id, actor="person")["code"] == "not_found"

    still = outcomes.get("bob", oid)
    assert still["title"] == "Bob's private thing" and still["state"] == "open" and len(still["links"]) == 1


@pytest.mark.security
def test_an_empty_owner_reads_and_writes_nothing(outcomes):
    """A missing identity must not fall through to rows with a null or empty owner."""
    assert outcomes.create("", "x")["ok"] is False
    assert outcomes.create(None, "x")["ok"] is False
    assert outcomes.list_for("") == []
