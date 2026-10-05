"""The history routes read and write the stored messages of a chat.

Hidden messages (compaction summaries kept for the model's context) must stay
out of every response, including the one served from the database after a
restart. The stop, last-message-metadata and continue-merge handlers each
order the stored rows by their timestamp; ordering by a column the table does
not have raised AttributeError and turned Stop and Continue into HTTP 500s
(#1659). These tests run the handlers against stored rows.
"""
import json
from datetime import datetime, timedelta

import pytest

import src.database
from core.database import ChatMessage as StoredMessage


@pytest.fixture
def chat(api):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="ep-alice", name="model", base_url="http://model.test/v1", is_enabled=True,
            owner="alice", cached_models=json.dumps(["m"]),
        ))
        db.commit()
    finally:
        db.close()
    client = api.as_user("alice")
    created = client.post("/api/session", data={
        "name": "history", "skip_validation": "true", "endpoint_id": "ep-alice", "model": "m"})
    assert created.status_code == 200, created.text
    client.session_id = created.json()["id"]
    return client


def _store(session_id, *rows):
    """Write stored rows directly: (role, content, metadata)."""
    db = src.database.SessionLocal()
    try:
        start = datetime(2026, 6, 1, 12, 0)
        for index, (role, content, metadata) in enumerate(rows):
            db.add(StoredMessage(
                id=f"{session_id}-{index}", session_id=session_id, role=role, content=content,
                meta_data=json.dumps(metadata) if metadata else None,
                timestamp=start + timedelta(minutes=index),
            ))
        db.commit()
    finally:
        db.close()


def _stored(session_id):
    db = src.database.SessionLocal()
    try:
        rows = db.query(StoredMessage).filter(StoredMessage.session_id == session_id).order_by(StoredMessage.timestamp)
        return [(r.role, r.content, json.loads(r.meta_data) if r.meta_data else {}) for r in rows]
    finally:
        db.close()


def test_a_hidden_message_is_not_served_from_the_database_fallback(chat):
    _store(chat.session_id, ("assistant", "COMPACTION SUMMARY", {"hidden": True}))

    response = chat.get(f"/api/history/{chat.session_id}")

    assert response.status_code == 200
    assert response.json()["history"] == []


def test_a_hidden_message_is_not_served_in_a_paged_read(chat):
    _store(chat.session_id,
           ("user", "hello", None),
           ("assistant", "COMPACTION SUMMARY", {"hidden": True}),
           ("assistant", "hi there", None))

    response = chat.get(f"/api/history/{chat.session_id}", params={"limit": 10})

    assert [m["content"] for m in response.json()["history"]] == ["hello", "hi there"]


def test_stop_marks_the_newest_assistant_message_in_the_database(chat):
    _store(chat.session_id, ("user", "q", None), ("assistant", "first", None),
           ("user", "q2", None), ("assistant", "second", None))

    response = chat.post(f"/api/session/{chat.session_id}/mark-stopped")

    assert response.status_code == 200, response.text
    rows = _stored(chat.session_id)
    assert rows[-1][2].get("stopped") is True
    assert "stopped" not in rows[1][2]


def test_last_message_metadata_is_saved_to_the_newest_assistant_row(chat):
    _store(chat.session_id, ("user", "q", None), ("assistant", "first", None), ("assistant", "second", None))

    response = chat.post(f"/api/session/{chat.session_id}/update-last-meta",
                         json={"metadata": {"variants": ["a", "b"]}})

    assert response.status_code == 200, response.text
    rows = _stored(chat.session_id)
    assert rows[-1][2].get("variants") == ["a", "b"]
    assert "variants" not in rows[1][2]


def test_continue_merges_the_last_two_assistant_rows(chat):
    _store(chat.session_id, ("user", "q", None), ("assistant", "The answer is", None),
           ("assistant", "forty-two.", None))

    response = chat.post(f"/api/session/{chat.session_id}/merge-last-assistant", json={"separator": " "})

    assert response.status_code == 200, response.text
    assert [(role, content) for role, content, _ in _stored(chat.session_id)] == [
        ("user", "q"), ("assistant", "The answer is forty-two.")]
