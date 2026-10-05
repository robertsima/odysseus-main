from types import SimpleNamespace

from core.database import ChatMessage as DbChatMessage
from core.models import ChatMessage
from core.session_manager import SessionManager
import core.session_manager as SM


def _manager_with(sessions):
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = dict(sessions)
    return manager


def _stored_messages(app_db):
    db = app_db.SessionLocal()
    try:
        return db.query(DbChatMessage).count()
    finally:
        db.close()


def test_persist_message_drops_write_when_parent_session_is_gone(monkeypatch, app_db):
    monkeypatch.setattr(SM, "SessionLocal", app_db.SessionLocal)
    manager = _manager_with({"deleted": SimpleNamespace(history=[])})
    message = ChatMessage("assistant", "late token")

    manager._persist_message("deleted", message)

    assert "deleted" not in manager.sessions
    assert _stored_messages(app_db) == 0
