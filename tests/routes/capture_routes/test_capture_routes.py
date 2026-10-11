"""/api/capture over HTTP: the real app, auth middleware and users alice, bob and admin."""
import pytest

BODY = {"text": "pay the electricity bill friday", "tz_offset": 0}


def test_capture_requires_a_signed_in_user(api, td, notes):
    res = api.anonymous().post("/api/capture", json=BODY)

    assert res.status_code == 401 and td.calls == []


def test_an_empty_line_is_a_400(api, td, notes):
    res = api.as_user("alice").post("/api/capture", json={"text": "   "})

    assert res.status_code == 400


@pytest.mark.security
def test_a_regular_user_gets_a_note_and_td_is_never_called(api, td, notes):
    """Production bug: Todoist is one shared account; a regular user's task must not land in the admin's list."""
    res = api.as_user("alice").post("/api/capture", json=BODY)

    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["status"], body["destination"], body["fallback_from"]) == ("created", "note", "todoist")
    assert td.calls == []
    [note] = notes.notes.values()
    assert note.owner == "alice"


def test_the_admin_gets_a_todoist_task(api, td, notes):
    res = api.as_admin().post("/api/capture", json=BODY)

    body = res.json()
    assert (body["status"], body["destination"]) == ("created", "todoist")
    assert td.creates == 1 and notes.notes == {}


@pytest.mark.security
def test_bob_cannot_replay_or_move_alices_capture(api, td, notes):
    alice = api.as_user("alice")
    bob = api.as_user("bob")
    first = alice.post("/api/capture", json={**BODY, "idempotency_key": "same"}).json()

    theirs = bob.post("/api/capture", json={**BODY, "text": "water the plants saturday", "idempotency_key": "same"}).json()
    moved = bob.post("/api/capture/same/move", json={"to": "calendar"})
    unknown = bob.post("/api/capture/never-made/move", json={"to": "calendar"})

    assert theirs["ref"] != first["ref"]
    assert moved.status_code == 200 and moved.json()["destination"] == "calendar"
    assert unknown.status_code == 404
    assert notes.find(first["ref"], "alice") is not None  # alice's note is untouched by bob's move


@pytest.mark.security
def test_a_key_that_only_alice_made_is_not_found_for_bob(api, td, notes):
    api.as_user("alice").post("/api/capture", json={**BODY, "idempotency_key": "only-alice"})

    res = api.as_user("bob").post("/api/capture/only-alice/move", json={"to": "calendar"})

    assert res.status_code == 404


def test_move_after_the_window_is_a_409(api, td, notes, monkeypatch):
    from datetime import datetime, timedelta
    from src import capture

    alice = api.as_user("alice")
    alice.post("/api/capture", json={**BODY, "idempotency_key": "late"})
    monkeypatch.setattr(capture, "_utcnow", lambda: datetime.utcnow() + timedelta(minutes=45))

    res = alice.post("/api/capture/late/move", json={"to": "calendar"})

    assert res.status_code == 409
    assert len(notes.notes) == 1


def test_a_clarification_is_a_200_with_options_and_writes_nothing(api, td, notes):
    res = api.as_admin().post("/api/capture", json={"text": "dentist friday", "tz_offset": 0})

    body = res.json()
    assert res.status_code == 200 and body["status"] == "needs_clarification" and body["options"]
    assert td.calls == [] and notes.notes == {}
