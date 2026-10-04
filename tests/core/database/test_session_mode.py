"""get_session_mode and set_session_mode give their connection back, even on error.

The chat route reads and writes a chat's mode on every turn. These helpers
swallow database errors (the turn goes on without the mode), and they used to
hand-roll SessionLocal() with close() as the last statement of the try, so an
error before it ("database is locked" under concurrent streams) leaked the
connection. A pool that leaks one per error runs dry, and from then on no
request gets a database session until a restart.

The pool here holds one connection and waits a second for it, so a leak shows
as a checked-out connection and the next call fails fast instead of hanging.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

import core.database as database


@pytest.fixture
def db(make_test_db, monkeypatch):
    test_db = make_test_db(poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=1)
    monkeypatch.setattr(database, "SessionLocal", test_db.SessionLocal)
    session = test_db.SessionLocal()
    session.add(database.Session(id="s1", name="chat", endpoint_url="http://127.0.0.1:9/v1", model="m"))
    session.commit()
    session.close()
    return test_db


def test_the_mode_is_saved_and_read_back(db):
    assert database.set_session_mode("s1", "agent") is True
    assert database.get_session_mode("s1") == "agent"
    assert db.engine.pool.checkedout() == 0


def test_a_database_error_is_swallowed_and_the_connection_returned(db):
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE sessions"))

    assert database.set_session_mode("s1", "agent") is False
    assert db.engine.pool.checkedout() == 0
    assert database.get_session_mode("s1") is None
    assert db.engine.pool.checkedout() == 0
