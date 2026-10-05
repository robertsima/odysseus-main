import pytest

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

from core.database import Session, ChatMessage
from datetime import datetime

def test_sqlite_foreign_keys_cascade(make_test_db):
    # A fresh SQLite connection has foreign keys off; the copy's engine opens
    # one, so the cascade below only happens if core.database's connect
    # listener turns them on.
    TestSessionLocal = make_test_db(memory=True).SessionLocal
    db = TestSessionLocal()
    
    session_id = "test-session-123"
    s = Session(
        id=session_id,
        name="Test Session",
        endpoint_url="http://localhost:8000",
        model="gpt-4",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    m = ChatMessage(id="test-msg-123", session_id=session_id, role="user", content="test message")
    
    db.add(s)
    db.add(m)
    db.commit()
    
    assert db.query(Session).count() == 1
    assert db.query(ChatMessage).count() == 1
    
    db.query(Session).filter(Session.id == session_id).delete()
    db.commit()
    
    assert db.query(ChatMessage).count() == 0
    
    db.close()
