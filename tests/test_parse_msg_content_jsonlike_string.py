"""Persistence contracts for JSON-like text and multimodal chat content.

Plain text that resembles a JSON content-block list must remain an exact
string. Real provider multimodal blocks follow the durable attachment
contract: readable text plus stable attachment metadata is persisted, while
raw inline media bytes are omitted.
"""
import uuid

import pytest

import core.database as cdb
from core.database import Session as DbSession
from core.models import ChatMessage

@pytest.fixture
def manager(monkeypatch, app_db):
    import core.session_manager as sm
    monkeypatch.setattr(sm, "SessionLocal", app_db.SessionLocal)
    mgr = sm.SessionManager.__new__(sm.SessionManager)
    mgr.sessions = {}
    return mgr


def _make_session(app_db, sid, owner="alice"):
    db = app_db.SessionLocal()
    try:
        db.add(DbSession(id=sid, owner=owner, name="chat",
                         endpoint_url="http://x", model="gpt-4o",
                         archived=False, message_count=1))
        db.commit()
    finally:
        db.close()


def test_jsonlike_user_string_not_corrupted(manager, app_db):
    sid = "sess-" + uuid.uuid4().hex[:8]
    _make_session(app_db, sid)
    text = '[{"type": "object", "name": "foo"}]'
    msgs = [ChatMessage(role="user", content=text)]
    assert manager.replace_messages(sid, msgs) is True

    manager.sessions.clear()
    reloaded = manager.get_session(sid)
    # Must come back as the ORIGINAL STRING, not silently parsed into a list.
    assert isinstance(reloaded.history[0].content, str)
    assert reloaded.history[0].content == text


def test_real_multimodal_content_persists_reference_without_base64(manager, app_db):
    sid = "sess-" + uuid.uuid4().hex[:8]
    _make_session(app_db, sid)
    attachment_id = "a" * 32 + ".png"
    multimodal = [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    metadata = {
        "attachments": [
            {
                "id": attachment_id,
                "name": "diagram.png",
                "mime": "image/png",
                "size": 4,
                "checksum_sha256": "sha256-digest",
            }
        ]
    }
    msgs = [ChatMessage(role="user", content=multimodal, metadata=metadata)]
    assert manager.replace_messages(sid, msgs) is True

    expected = (
        "what is this?\n"
        "[1 inline media payload omitted]\n"
        f"[Attachment: diagram.png | id={attachment_id} | mime=image/png | "
        "size=4 bytes | sha256=sha256-digest]"
    )

    db = app_db.SessionLocal()
    try:
        stored = db.query(cdb.ChatMessage).filter_by(session_id=sid).one()
        assert stored.content == expected
        assert "what is this?" in stored.content
        assert attachment_id in stored.content
        assert "data:image/png;base64,AAAA" not in stored.content
        assert "base64" not in stored.content
        assert "AAAA" not in stored.content
    finally:
        db.close()

    manager.sessions.clear()
    reloaded = manager.get_session(sid)
    assert reloaded.history[0].content == expected
    assert reloaded.history[0].metadata["attachments"][0]["id"] == attachment_id
