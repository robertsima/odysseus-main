"""Drive the real Odysseus app over HTTP, logged in as a known user.

The ``api`` fixture imports ``app`` once per process. That builds the FastAPI
app with every route and the real AuthMiddleware, so a request from a test
passes the same auth, admin and owner checks it would in production. Each test
gets fresh accounts on a throwaway auth file: an admin called ``admin`` and
two regular users, ``alice`` and ``bob``.

    def test_bob_does_not_see_alice_sessions(api):
        alice, bob = api.as_user("alice"), api.as_user("bob")
        alice.post("/api/session", data={"name": "plans", "skip_validation": "true"})
        assert bob.get("/api/sessions").json() == []

``api.as_admin()``, ``api.as_user(name)`` and ``api.anonymous()`` each return a
``TestClient`` carrying that user's session cookie (or none); keyword arguments
go to ``TestClient``. ``api.auth`` is the AuthManager in use, ``api.module`` the
imported app.py and ``api.session_manager`` the app's session manager. The
lifespan does not run, so no scheduler or background service starts. Each test
starts with an empty database and session cache.

The repo's data folder is never read or written. tests/conftest.py imports
core.database, and with it src.constants, before this plugin loads, so every
DATA_DIR-derived path is already fixed to the real folder by then, and test
collection copies those paths into most project modules. For the app import
and for each test the fixture repoints every such path in a loaded project
module at a per-process temp folder, and a file-access guard fails the test if
anything still opens, lists or changes a file under the real data folder.
"""
from __future__ import annotations

import inspect
import os
import shutil
import sys
from pathlib import Path, PurePath

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADMIN = "admin"
USERS = ("alice", "bob")
PASSWORD = "test-password-123"

# Audit events that touch a path, and which arguments hold paths.
_PATH_EVENTS = {
    "open": (0,), "os.listdir": (0,), "os.scandir": (0,), "os.mkdir": (0,),
    "os.rmdir": (0,), "os.remove": (0,), "os.rename": (0, 1), "os.truncate": (0,),
    "os.chmod": (0,), "os.utime": (0,), "os.symlink": (0, 1), "os.link": (0, 1),
    "shutil.rmtree": (0,), "shutil.copyfile": (0, 1), "shutil.move": (0, 1),
    "sqlite3.connect": (0,),
}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _protected_dirs() -> tuple[str, ...]:
    """The folder a run would use for real data: the repo's data/ and DATA_DIR."""
    import src.constants as constants
    from src.runtime_paths import get_default_data_dir

    dirs = set()
    for path in (get_default_data_dir(), constants.DATA_DIR):
        dirs.add(_norm(path))
        dirs.add(os.path.normcase(os.path.realpath(path)))
    return tuple(sorted(dirs))


# Computed when the plugin loads, before any fixture repoints src.constants.
_PROTECTED = _protected_dirs()
_guard = {"active": False, "installed": False, "hits": []}


def _under_protected(path) -> bool:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    elif isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path or path == ":memory:":
        return False
    try:
        norm = _norm(path)
    except (TypeError, ValueError):
        return False
    return any(norm == d or norm.startswith(d + os.sep) for d in _PROTECTED)


def _audit(event, args):
    if not _guard["active"]:
        return
    for index in _PATH_EVENTS.get(event, ()):
        if index < len(args) and _under_protected(args[index]):
            _guard["hits"].append(f"{event} {args[index]!r}")
            raise PermissionError(f"test touched the real data folder: {event} {args[index]!r}")


def _install_guard():
    # Audit hooks can't be removed, so this one is installed once and stays
    # idle unless an api test is running.
    if not _guard["installed"]:
        sys.addaudithook(_audit)
        _guard["installed"] = True


def _rebase(value, temp_dir: str):
    """``value`` moved from a protected folder into ``temp_dir``, or None."""
    if isinstance(value, PurePath):
        moved = _rebase(str(value), temp_dir)
        return type(value)(moved) if moved is not None else None
    if not isinstance(value, str) or not os.path.isabs(value):
        return None
    absolute = os.path.abspath(value)
    norm = os.path.normcase(absolute)
    for protected in _PROTECTED:
        if norm == protected or norm.startswith(protected + os.sep):
            return temp_dir + absolute[len(protected):]
    return None


def _project_modules():
    root = os.path.normcase(str(ROOT)) + os.sep
    skipped = (root + "tests" + os.sep, root + "node_modules" + os.sep)
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        norm = os.path.normcase(os.path.abspath(file))
        if norm.startswith(root) and not norm.startswith(skipped) and "site-packages" not in norm:
            yield module


def _functions_of(module):
    """Functions and methods defined in ``module``, including decorated ones."""
    for value in list(vars(module).values()):
        if not (inspect.isfunction(value) or inspect.isclass(value)):
            continue
        if value.__module__ != module.__name__:
            continue
        members = list(vars(value).values()) if inspect.isclass(value) else [value]
        for member in members:
            func = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
            if inspect.isfunction(func):
                yield func
                try:
                    inner = inspect.unwrap(func)
                except ValueError:
                    continue
                if inner is not func and inspect.isfunction(inner):
                    yield inner


def _repoint_defaults(monkeypatch, func, temp_dir: str) -> None:
    defaults = func.__defaults__
    if defaults:
        moved = tuple(_rebase(v, temp_dir) for v in defaults)
        if any(m is not None for m in moved):
            new = tuple(v if m is None else m for v, m in zip(defaults, moved))
            monkeypatch.setattr(func, "__defaults__", new)
    kwdefaults = func.__kwdefaults__
    if kwdefaults:
        moved_kw = {k: _rebase(v, temp_dir) for k, v in kwdefaults.items()}
        if any(m is not None for m in moved_kw.values()):
            new_kw = {k: v if moved_kw[k] is None else moved_kw[k] for k, v in kwdefaults.items()}
            monkeypatch.setattr(func, "__kwdefaults__", new_kw)


def _repoint_data_paths(monkeypatch, temp_dir: str) -> None:
    """Point every DATA_DIR-derived path that project code holds at ``temp_dir``.

    That covers module attributes (``from src.constants import SESSIONS_FILE``)
    and default arguments bound at import (``cache_dir=TTS_CACHE_DIR``).
    """
    for module in _project_modules():
        for name, value in list(vars(module).items()):
            moved = _rebase(value, temp_dir)
            if moved is not None:
                monkeypatch.setattr(module, name, moved)
        for func in _functions_of(module):
            _repoint_defaults(monkeypatch, func, temp_dir)


_schema = {"template": None}


def _bind_test_database(monkeypatch):
    """Give the test its own in-memory database with the app's schema.

    The suite's default DATABASE_URL is ``sqlite:///:memory:``, and SQLAlchemy
    keeps one connection per thread for it, so a sync route running in the
    threadpool sees an empty database ("no such table"). Until the shared
    database fixture (plan D21) replaces this, every session the app opens goes
    to one connection that is copied from a schema built once per process.

    Every project module's ``SessionLocal`` is replaced, not only
    core.database's: modules hold their own reference, and some test modules
    swap theirs for a stub while they are collected.
    """
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database

    if _schema["template"] is None:
        template = sqlite3.connect(":memory:", check_same_thread=False)
        builder = create_engine("sqlite://", creator=lambda: template, poolclass=StaticPool)
        core.database.Base.metadata.create_all(builder)
        _schema["template"] = template
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    _schema["template"].backup(conn)
    engine = create_engine("sqlite://", creator=lambda: conn, poolclass=StaticPool)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    old_engine = core.database.engine
    for module in _project_modules():
        for name, value in list(vars(module).items()):
            if name == "SessionLocal":
                monkeypatch.setattr(module, name, factory)
            elif value is old_engine:
                monkeypatch.setattr(module, name, engine)
    return conn


def _set_app_env(monkeypatch, data_dir: str) -> None:
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", data_dir)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "false")


_runtime = {"module": None, "auth_manager": None}


def _import_app(monkeypatch):
    """Import app.py once per process, with auth on and no .env or MCP servers."""
    module = _runtime["module"]
    if module is not None:
        return module
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("ODYSSEUS_DISABLE_MCP", "1")
    monkeypatch.setenv("CHROMADB_HOST", "127.0.0.1")
    monkeypatch.setenv("CHROMADB_PORT", "9")
    monkeypatch.setenv("CHROMADB_CONNECT_TIMEOUT", "0.01")
    # Another test may have imported app.py with other settings (auth off,
    # the real data folder). Build a fresh one under ours.
    sys.modules.pop("app", None)
    import app as module

    names = {m.cls.__name__ for m in module.app.user_middleware}
    if "AuthMiddleware" not in names:
        raise RuntimeError("app.py was imported without AuthMiddleware; AUTH_ENABLED must not be false")
    _runtime["module"] = module
    _runtime["auth_manager"] = module.auth_manager
    return module


@pytest.fixture(scope="session")
def _odysseus_test_data_dir(tmp_path_factory):
    """A per-process stand-in for the data folder, with a template auth file.

    The three accounts are created once here because each password hash takes
    a few hundred milliseconds of bcrypt.
    """
    from core.auth import AuthManager

    data_dir = tmp_path_factory.mktemp("odysseus-data")
    template = tmp_path_factory.mktemp("odysseus-auth") / "auth.json"
    manager = AuthManager(auth_path=str(template))
    manager.create_user(ADMIN, PASSWORD, is_admin=True)
    for name in USERS:
        manager.create_user(name, PASSWORD)
    return str(data_dir), template


class HttpApp:
    """The running app plus logged-in clients. Built by the ``api`` fixture."""

    def __init__(self, module, auth_manager):
        self.module = module
        self.app = module.app
        self.auth = auth_manager
        self._clients = []

    @property
    def session_manager(self):
        return self.app.state.session_manager

    def _client(self, username=None, **kwargs):
        from fastapi.testclient import TestClient
        from routes.auth_routes import SESSION_COOKIE

        client = TestClient(self.app, **kwargs)
        if username is not None:
            token = self.auth.create_session_trusted(username)
            if token is None:
                raise ValueError(f"no such test user: {username!r}")
            client.cookies.set(SESSION_COOKIE, token)
        self._clients.append(client)
        return client

    def as_user(self, username: str, **kwargs):
        """A client logged in as ``username`` (``alice``, ``bob`` or ``admin``)."""
        return self._client(username, **kwargs)

    def as_admin(self, **kwargs):
        return self._client(ADMIN, **kwargs)

    def anonymous(self, **kwargs):
        return self._client(None, **kwargs)

    def close(self):
        for client in self._clients:
            client.close()
        self._clients.clear()


@pytest.fixture
def api(monkeypatch, _odysseus_test_data_dir):
    """The real app over HTTP with users ``admin`` (admin), ``alice`` and ``bob``."""
    data_dir, template = _odysseus_test_data_dir
    _install_guard()
    _set_app_env(monkeypatch, data_dir)
    _repoint_data_paths(monkeypatch, data_dir)

    _guard["hits"] = []
    _guard["active"] = True
    try:
        module = _import_app(monkeypatch)
        # Modules first imported by app.py picked up the temp paths already;
        # this catches copies made by modules loaded earlier.
        _repoint_data_paths(monkeypatch, data_dir)
        database = _bind_test_database(monkeypatch)

        import core.auth

        auth_file = os.path.join(data_dir, "auth.json")
        for leftover in ("auth.json", "sessions.json"):
            Path(data_dir, leftover).unlink(missing_ok=True)
        shutil.copyfile(template, auth_file)
        core.auth.reset_shared_auth_managers()
        # The auth and webhook routes keep the AuthManager they were built
        # with, so swap that object's state for a fresh one rather than
        # swapping the object.
        manager = _runtime["auth_manager"]
        fresh = core.auth.AuthManager(auth_path=auth_file)
        monkeypatch.setattr(manager, "__dict__", fresh.__dict__)
        monkeypatch.setattr(module, "auth_manager", manager)
        monkeypatch.setattr(module.app.state, "auth_manager", manager, raising=False)
        monkeypatch.setattr(module, "LOCALHOST_BYPASS", False)
        # The session manager caches chats in memory for the process; start
        # each test with none, to match the empty database.
        monkeypatch.setattr(module.app.state.session_manager, "sessions", {})

        http = HttpApp(module, manager)
        try:
            yield http
        finally:
            http.close()
            database.close()
            core.auth.reset_shared_auth_managers()
    finally:
        _guard["active"] = False
    hits = list(_guard["hits"])
    if hits:
        pytest.fail("the test touched the real data folder:\n  " + "\n  ".join(hits[:20]))
