"""Test databases: each process's default database and fresh copies per test.

The process default. core.database builds its engine from DATABASE_URL when it
is imported. The suite used to default that to ``sqlite:///:memory:``, and
SQLAlchemy pools a memory URL per thread: every thread got its own empty
database, and once five threads had connected the pool closed one connection
at random, sometimes the main thread's. Tests that reached the database from a
worker thread, or ran after one that did, failed with "no such table". Each
test process now gets its own SQLite file, shared by all of its threads, in a
temporary folder (on tmpfs when the machine has one) that is removed at exit.

Fresh copies. Building core.database's schema runs about 115 DDL statements,
each committed on its own; on a disk with slow fsync that took 3.4 s, and 73
test files built one per test or per module. Here each schema is built once
per process in memory, and a test that asks for a database gets a copy made
with sqlite3's backup API, deleted after the test. Use the ``app_db`` fixture
for core.database's schema, or ``make_test_db`` for another schema, an
in-memory copy, more than one database, or engine options.

Every SQLite connection SQLAlchemy opens in a test process also runs with
``synchronous=OFF`` and ``journal_mode=MEMORY``. A test database never has to
survive a crash, and without them each commit waits for the disk.
"""
from __future__ import annotations

import atexit
import itertools
import os
import shutil
import sqlite3
import sys
import tempfile
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

# Set alongside DATABASE_URL when this module picked the URL, so a child
# process (an xdist worker inherits the controller's environment) can tell an
# inherited default from one the user chose, and pick its own file.
_AUTO_URL_ENV = "ODYSSEUS_TEST_AUTO_DATABASE_URL"
_FAST_PRAGMAS = ("PRAGMA synchronous=OFF", "PRAGMA journal_mode=MEMORY")

_run_dir: Optional[str] = None
_pragmas_installed = False
_copy_numbers = itertools.count()
# MetaData -> (template connection, the engine that built it). Weak keys: a
# schema declared inside one test is dropped with it.
_templates: "weakref.WeakKeyDictionary[Any, tuple]" = weakref.WeakKeyDictionary()
# Copies made outside a fixture (tests/helpers/sqlite_db.py); closed at exit.
_unscoped: list["TempDatabase"] = []


def run_dir() -> str:
    """This process's folder for test databases, created on first use."""
    global _run_dir
    if _run_dir is None:
        shm = "/dev/shm"
        base = shm if os.path.isdir(shm) and os.access(shm, os.W_OK) else None
        _run_dir = tempfile.mkdtemp(prefix=f"odysseus-tests-{os.getpid()}-", dir=base)
        atexit.register(_remove_run_dir)
    return _run_dir


def _remove_run_dir() -> None:
    for db in reversed(_unscoped):
        db.close()
    database = sys.modules.get("core.database")
    engine = getattr(database, "engine", None)
    if engine is not None:
        try:
            engine.dispose()
        except Exception:
            pass
    for conn, _engine in list(_templates.values()):
        conn.close()
    if _run_dir is not None:
        shutil.rmtree(_run_dir, ignore_errors=True)


def _set_fast_pragmas(dbapi_connection, connection_record) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _FAST_PRAGMAS:
            try:
                cursor.execute(pragma)
            except sqlite3.OperationalError:
                pass  # a locked or read-only database keeps its own mode
    finally:
        cursor.close()


def install_fast_pragmas() -> None:
    """Apply the fast pragmas to every SQLite connection SQLAlchemy opens."""
    global _pragmas_installed
    if _pragmas_installed:
        return
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    event.listen(Engine, "connect", _set_fast_pragmas)
    _pragmas_installed = True


def use_process_database() -> None:
    """Give this process its own SQLite file as core.database's default.

    Call it before core.database is imported. A DATABASE_URL set by the user
    is kept; one that this function set in a parent process is replaced.
    """
    install_fast_pragmas()
    current = os.environ.get("DATABASE_URL")
    if current and current != os.environ.get(_AUTO_URL_ENV):
        return
    url = "sqlite:///" + Path(run_dir(), "app.db").as_posix()
    os.environ["DATABASE_URL"] = url
    os.environ[_AUTO_URL_ENV] = url


def _template(metadata) -> sqlite3.Connection:
    entry = _templates.get(metadata)
    if entry is None:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        conn = sqlite3.connect(":memory:", check_same_thread=False)
        engine = create_engine("sqlite://", creator=lambda: conn, poolclass=StaticPool)
        metadata.create_all(engine)
        entry = (conn, engine)
        _templates[metadata] = entry
    return entry[0]


@dataclass
class TempDatabase:
    """A fresh copy of a schema.

    ``SessionLocal`` is a sessionmaker bound to ``engine``. ``path`` is the
    SQLite file, or None for an in-memory copy.
    """

    engine: Any
    SessionLocal: Any
    path: Optional[str]
    _conn: Optional[sqlite3.Connection] = None

    @property
    def url(self) -> str:
        return self.engine.url.render_as_string(hide_password=False)

    def close(self) -> None:
        """Dispose of the engine and delete the copy."""
        self.engine.dispose()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self.path:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(self.path + suffix)
                except OSError:
                    pass


def new_database(metadata=None, *, memory: bool = False, **engine_kwargs) -> TempDatabase:
    """Copy ``metadata``'s schema (core.database's by default) into a new database.

    A file copy gets a NullPool engine unless ``engine_kwargs`` say otherwise,
    so each session opens its own connection, as with a production file
    database. ``memory=True`` gives an in-memory copy behind one connection
    that every thread shares. The caller closes the result.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool, StaticPool

    if metadata is None:
        from core.database import Base

        metadata = Base.metadata
    template = _template(metadata)
    if memory:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        template.backup(conn)
        engine = create_engine("sqlite://", creator=lambda: conn, poolclass=StaticPool, **engine_kwargs)
        path = None
    else:
        conn = None
        path = os.path.join(run_dir(), f"copy-{next(_copy_numbers)}.db")
        dest = sqlite3.connect(path)
        try:
            dest.execute("PRAGMA synchronous=OFF")
            template.backup(dest)
        finally:
            dest.close()
        engine_kwargs.setdefault("poolclass", NullPool)
        engine_kwargs.setdefault("connect_args", {"check_same_thread": False})
        engine = create_engine("sqlite:///" + Path(path).as_posix(), **engine_kwargs)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return TempDatabase(engine, SessionLocal, path, conn)


def new_unscoped_database(metadata=None, **kwargs) -> TempDatabase:
    """``new_database`` for code that cannot take a fixture; closed at exit."""
    db = new_database(metadata, **kwargs)
    _unscoped.append(db)
    return db


@pytest.fixture
def make_test_db():
    """Factory for fresh database copies, each deleted after the test.

    ``make_test_db()`` returns a TempDatabase holding a file copy of
    core.database's schema. ``make_test_db(metadata)`` copies another schema,
    ``memory=True`` makes an in-memory copy, and other keyword arguments go to
    ``create_engine``.
    """
    made: list[TempDatabase] = []

    def make(metadata=None, **kwargs) -> TempDatabase:
        db = new_database(metadata, **kwargs)
        made.append(db)
        return db

    yield make
    for db in reversed(made):
        db.close()


@pytest.fixture
def app_db(make_test_db) -> TempDatabase:
    """A fresh file copy of core.database's schema, deleted after the test."""
    return make_test_db()
