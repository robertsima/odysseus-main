"""An existing install gains the CalDAV sync columns when it starts on the new code.

Databases created before bidirectional CalDAV sync have calendar_events and
calendars tables without remote_href, remote_etag, caldav_sync_pending and
caldav_base_url. If the startup migration skips them, every calendar query
fails with "no such column" and the calendar page is empty after an update.
"""
import sqlite3

import core.database as database


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_the_migration_adds_the_remote_columns_to_a_pre_caldav_database(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calendars (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE calendar_events (uid TEXT PRIMARY KEY, summary TEXT)")
    conn.execute("INSERT INTO calendar_events VALUES ('e1', 'Existing event')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{path}")

    database._migrate_add_caldav_sync_columns()
    database._migrate_add_caldav_sync_columns()  # a second start changes nothing

    assert {"remote_href", "remote_etag", "caldav_sync_pending"} <= _columns(path, "calendar_events")
    assert "caldav_base_url" in _columns(path, "calendars")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT summary FROM calendar_events").fetchall() == [("Existing event",)]
    finally:
        conn.close()

