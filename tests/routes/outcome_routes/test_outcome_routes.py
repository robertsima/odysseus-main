"""/api/outcomes over HTTP: the real app, auth middleware and three users."""
import pytest

from src import outcomes as store
from src.blockers import Blocker


def _create(client, **body):
    body.setdefault("title", "Ship the importer")
    res = client.post("/api/outcomes", json=body)
    assert res.status_code == 200, res.text
    return res.json()["outcome"]


def test_create_then_list_with_a_state_filter(api):
    alice = api.as_user("alice")
    a = _create(alice, title="Renew passport", kind="obligation", priority=2)
    b = _create(alice, title="Build capture", kind="project", state="active")

    everything = alice.get("/api/outcomes").json()["outcomes"]
    active = alice.get("/api/outcomes", params={"state": "active"}).json()["outcomes"]

    assert {o["id"] for o in everything} == {a["id"], b["id"]}
    assert [o["id"] for o in active] == [b["id"]]
    assert a["priority"] == 2 and a["source"] == "person" and a["state"] == "open"


def test_create_without_a_title_is_a_400(api):
    res = api.as_user("alice").post("/api/outcomes", json={"title": "   "})

    assert res.status_code == 400


def test_get_returns_links_evidence_and_events(api):
    alice = api.as_user("alice")
    out = _create(alice)
    alice.post(f"/api/outcomes/{out['id']}/links", json={"kind": "todoist_task", "ref": "8431", "label": "Pay rent"})
    store.add_evidence("alice", out["id"], {"kind": "commit", "ref": "abc1234", "detail": "fix"}, actor="agent:w1")

    got = alice.get(f"/api/outcomes/{out['id']}").json()["outcome"]

    assert [(link["kind"], link["ref"]) for link in got["links"]] == [("todoist_task", "8431")]
    assert got["evidence"][0]["ref"] == "abc1234"
    assert [e["kind"] for e in got["events"]] == ["created", "link", "evidence"]


def test_patch_edits_the_editable_fields_and_can_clear_one(api):
    alice = api.as_user("alice")
    out = _create(alice, next_action="Call the bank")

    res = alice.patch(f"/api/outcomes/{out['id']}", json={
        "title": "Renew passport", "detail": "Form DS-82", "next_action": None, "waiting_on": "Dana",
        "follow_up_at": "2026-10-14T09:00:00", "due_at": "2026-10-20T00:00:00", "priority": 1,
        "snoozed_until": "2026-10-12T09:00:00"})

    got = res.json()["outcome"]
    assert res.status_code == 200
    assert got["title"] == "Renew passport" and got["detail"] == "Form DS-82"
    assert got["next_action"] is None and got["waiting_on"] == "Dana"
    assert got["follow_up_at"].startswith("2026-10-14") and got["due_at"].startswith("2026-10-20")
    assert got["priority"] == 1 and got["snoozed_until"].startswith("2026-10-12")


def test_patch_cannot_set_state_confirmation_or_evidence(api):
    """Production bug: a PATCH body could mark done and confirmed, skipping the evidence rule."""
    alice = api.as_user("alice")
    out = _create(alice)

    res = alice.patch(f"/api/outcomes/{out['id']}", json={
        "title": "Still editable", "state": "done", "confirmed_by_person": True,
        "evidence": [{"kind": "commit", "ref": "forged", "by": "person"}], "evidence_json": "[1]",
        "owner": "bob", "completed_at": "2026-10-01T00:00:00"})

    assert res.status_code == 200
    got = alice.get(f"/api/outcomes/{out['id']}").json()["outcome"]
    assert got["title"] == "Still editable"
    assert got["state"] == "open" and got["confirmed_by_person"] is False
    assert got["evidence"] == [] and got["completed_at"] is None and got["owner"] == "alice"


def test_patch_rejects_a_priority_outside_1_to_4(api):
    alice = api.as_user("alice")
    out = _create(alice)

    assert alice.patch(f"/api/outcomes/{out['id']}", json={"priority": 7}).status_code == 400


def test_state_done_without_evidence_or_confirm_is_refused(api):
    alice = api.as_user("alice")
    out = _create(alice)

    res = alice.post(f"/api/outcomes/{out['id']}/state", json={"state": "done"})

    assert res.status_code == 409
    assert "evidence" in res.json()["detail"]
    assert alice.get(f"/api/outcomes/{out['id']}").json()["outcome"]["state"] == "open"


def test_state_done_with_confirm_records_the_persons_confirmation(api):
    alice = api.as_user("alice")
    out = _create(alice)

    res = alice.post(f"/api/outcomes/{out['id']}/state", json={"state": "done", "confirm": True})

    got = res.json()["outcome"]
    assert res.status_code == 200
    assert got["state"] == "done" and got["confirmed_by_person"] is True and got["completed_at"]
    events = alice.get(f"/api/outcomes/{out['id']}").json()["outcome"]["events"]
    assert events[-1]["actor"] == "person"


def test_state_done_with_evidence_needs_no_confirm(api):
    alice = api.as_user("alice")
    out = _create(alice)
    store.add_evidence("alice", out["id"], {"kind": "tests", "ref": "129 passed"}, actor="agent:w1")

    res = alice.post(f"/api/outcomes/{out['id']}/state", json={"state": "done"})

    assert res.status_code == 200 and res.json()["outcome"]["confirmed_by_person"] is False


def test_state_waiting_needs_waiting_on(api):
    alice = api.as_user("alice")
    out = _create(alice)

    refused = alice.post(f"/api/outcomes/{out['id']}/state", json={"state": "waiting"})
    ok = alice.post(f"/api/outcomes/{out['id']}/state",
                    json={"state": "waiting", "waiting_on": "Dana", "follow_up_at": "2026-10-14T09:00:00"})

    assert refused.status_code == 400
    assert ok.json()["outcome"]["waiting_on"] == "Dana"


def test_unknown_state_is_a_400(api):
    alice = api.as_user("alice")
    out = _create(alice)

    assert alice.post(f"/api/outcomes/{out['id']}/state", json={"state": "finished"}).status_code == 400


def test_links_add_is_idempotent_and_remove_detaches(api):
    alice = api.as_user("alice")
    out = _create(alice)
    first = alice.post(f"/api/outcomes/{out['id']}/links", json={"kind": "chat", "ref": "s1"}).json()["link"]
    again = alice.post(f"/api/outcomes/{out['id']}/links", json={"kind": "chat", "ref": "s1"}).json()["link"]
    bad = alice.post(f"/api/outcomes/{out['id']}/links", json={"kind": "email", "ref": "x"})

    removed = alice.delete(f"/api/outcomes/{out['id']}/links/{first['id']}")
    missing = alice.delete(f"/api/outcomes/{out['id']}/links/{first['id']}")

    assert again["id"] == first["id"] and bad.status_code == 400
    assert removed.status_code == 200 and missing.status_code == 404
    assert alice.get(f"/api/outcomes/{out['id']}").json()["outcome"]["links"] == []


def test_blocker_clear_returns_a_blocked_outcome_to_active(api):
    alice = api.as_user("alice")
    out = _create(alice, state="active")
    store.set_blocker("alice", out["id"], Blocker(code="NEEDS_USER", summary="s", recovery="user",
                                                 user_action="Paste the key"), actor="agent:w1")
    assert alice.get(f"/api/outcomes/{out['id']}").json()["outcome"]["state"] == "blocked"

    res = alice.post(f"/api/outcomes/{out['id']}/blocker/clear")

    got = res.json()["outcome"]
    assert res.status_code == 200 and got["state"] == "active" and got["blocker"] is None


def test_unknown_id_is_a_404(api):
    assert api.as_user("alice").get("/api/outcomes/nope").status_code == 404


@pytest.mark.security
def test_another_user_gets_404_on_every_route(api):
    """Owner scope: bob must not read, edit, move, link or unblock alice's outcome."""
    alice, bob = api.as_user("alice"), api.as_user("bob")
    out = _create(alice, title="Alice only", state="active")
    oid = out["id"]
    link = alice.post(f"/api/outcomes/{oid}/links", json={"kind": "chat", "ref": "s1"}).json()["link"]
    store.set_blocker("alice", oid, {"code": "X", "summary": "s", "recovery": "retry"}, actor="system")

    responses = [
        bob.get(f"/api/outcomes/{oid}"),
        bob.patch(f"/api/outcomes/{oid}", json={"title": "bob was here"}),
        bob.post(f"/api/outcomes/{oid}/state", json={"state": "done", "confirm": True}),
        bob.post(f"/api/outcomes/{oid}/links", json={"kind": "chat", "ref": "s2"}),
        bob.delete(f"/api/outcomes/{oid}/links/{link['id']}"),
        bob.post(f"/api/outcomes/{oid}/blocker/clear"),
    ]

    assert [r.status_code for r in responses] == [404] * 6
    assert bob.get("/api/outcomes").json()["outcomes"] == []
    still = alice.get(f"/api/outcomes/{oid}").json()["outcome"]
    assert still["title"] == "Alice only" and still["state"] == "blocked"
    assert len(still["links"]) == 1 and still["confirmed_by_person"] is False


@pytest.mark.security
def test_anonymous_callers_are_rejected(api):
    res = api.anonymous().get("/api/outcomes")

    assert res.status_code in (401, 403)
