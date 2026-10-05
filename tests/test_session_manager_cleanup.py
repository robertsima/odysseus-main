from datetime import datetime

from core.database import Session as DbSession
from core.session_manager import SessionManager
import core.session_manager as SM


def _manager_with(sessions=None):
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = dict(sessions or {})
    return manager


def _add_session(db, session_id, **fields):
    row = dict(name=session_id, endpoint_url="http://llm", model="m", archived=False)
    row.update(fields)
    db.add(DbSession(id=session_id, **row))


def test_cleanup_empty_sessions_archives_old_naive_last_accessed(monkeypatch, app_db):
    db = app_db.SessionLocal()
    _add_session(db, "old-chat", last_accessed=datetime(2026, 5, 1, 12, 0, 0), message_count=3)
    _add_session(db, "recent-chat", last_accessed=datetime(2026, 6, 3, 12, 0, 0), message_count=3)
    _add_session(
        db, "old-important", last_accessed=datetime(2026, 5, 1, 12, 0, 0), message_count=3,
        is_important=True,
    )
    db.commit()
    db.close()
    monkeypatch.setattr(SM, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(SM, "utcnow_naive", lambda: datetime(2026, 6, 4, 12, 0, 0))

    stats = _manager_with().cleanup_empty_sessions(auto_archive_days=30)

    assert stats == {"deleted_empty": 0, "archived_old": 1, "total_checked": 3}
    db = app_db.SessionLocal()
    try:
        archived = {s.id: s.archived for s in db.query(DbSession).all()}
    finally:
        db.close()
    assert archived == {"old-chat": True, "recent-chat": False, "old-important": False}
