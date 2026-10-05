"""A fresh SQLite copy of a schema for code that cannot take a fixture.

Prefer the ``app_db`` and ``make_test_db`` fixtures from
tests/plugins/database.py; they delete their copy after the test. This helper
makes the same copy, and the file is deleted when the test process exits. It
does not patch modules: the caller binds ``SessionLocal`` onto whatever module
the code under test reads.
"""
from tests.plugins.database import new_unscoped_database


def make_temp_sqlite(metadata=None):
    """Copy ``metadata``'s schema (core.database's by default) into a temp file.

    Returns ``(SessionLocal, engine, path)``. The engine uses NullPool, so
    each session opens its own connection to the file.
    """
    db = new_unscoped_database(metadata)
    return db.SessionLocal, db.engine, db.path
