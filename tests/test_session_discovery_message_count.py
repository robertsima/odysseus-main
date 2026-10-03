"""Real-SQLite regressions for session discovery with stale derived counts."""

import core.session_manager as session_manager


def _make_manager(app_db, monkeypatch):
    monkeypatch.setattr(session_manager, "SessionLocal", app_db.SessionLocal)
    return session_manager.SessionManager(), session_manager


def test_discovery_uses_persisted_rows_and_repairs_cached_count(app_db, monkeypatch):
    from core.models import ChatMessage

    manager, session_manager = _make_manager(app_db, monkeypatch)
    for session_id in ("stale-low", "empty", "stale-high"):
        manager.create_session(
            session_id=session_id,
            name=session_id,
            endpoint_url="http://example.invalid",
            model="test-model",
            rag=False,
            owner="tester",
        )
    manager.add_message("stale-low", ChatMessage("user", "persisted message"))

    db = session_manager.SessionLocal()
    try:
        db.query(session_manager.DbSession).filter(
            session_manager.DbSession.id == "stale-low"
        ).update({"message_count": 0})
        db.query(session_manager.DbSession).filter(
            session_manager.DbSession.id == "stale-high"
        ).update({"message_count": 7})
        db.commit()
    finally:
        db.close()

    restarted = session_manager.SessionManager()

    assert set(restarted.sessions) == {"stale-low"}
    assert restarted.sessions["stale-low"].history == []
    assert restarted.sessions["stale-low"].message_count == 1

    hydrated = restarted.get_session("stale-low")
    assert [message.content for message in hydrated.history] == ["persisted message"]
