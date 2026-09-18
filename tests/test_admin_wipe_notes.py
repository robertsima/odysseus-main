from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Note
from routes.admin_wipe_routes import setup_admin_wipe_routes
from src.notes_markdown import NoteRecord
from tests.helpers.fake_notes_store import FakeNotesStore


def test_wipe_notes_clears_markdown_store_and_legacy_rows(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    sessions = sessionmaker(bind=engine)
    db = sessions()
    db.add(Note(id="legacy", title="Legacy"))
    db.commit()
    db.close()

    store = FakeNotesStore(
        NoteRecord(id="active", title="Active"),
        NoteRecord(id="archived", title="Archived", archived=True),
    )
    import routes.admin_wipe_routes as routes
    import src.notes_store as notes_store

    monkeypatch.setattr(routes, "SessionLocal", sessions)
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(notes_store, "STORE", store)

    router = setup_admin_wipe_routes(session_manager=None)
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/admin/wipe/{kind}")
    result = endpoint(kind="notes", request=Request(scope={"type": "http"}))

    assert store.notes == {}
    db = sessions()
    assert db.query(Note).count() == 0
    db.close()
    assert result == {
        "status": "deleted",
        "kind": "notes",
        "count": 2,
        "legacy_rows_deleted": 1,
    }
