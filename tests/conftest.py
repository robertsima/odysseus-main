"""Shared test configuration - ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
import itertools
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Shared fixtures and guards live in tests/plugins/, one module per concern.
pytest_plugins = [
    "tests.plugins.network_guard",
    "tests.plugins.database",
    "tests.plugins.http_app",
]

# Importing core.database below runs init_db() at import time against
# DATABASE_URL. Unless DATABASE_URL is set, each test process gets its own
# SQLite file in a temporary folder, shared by all of its threads and removed
# at exit (tests/plugins/database.py). An explicit DATABASE_URL is preserved.
from tests.plugins.database import use_process_database  # noqa: E402

use_process_database()
# Never append test output to the deployment's data/logs/app.log.
os.environ["ODYSSEUS_FILE_LOG"] = "0"

# Pre-import real heavy modules BEFORE any test file's module-level stubs can
# replace them with MagicMock. Some test files (e.g. test_llm_core_sanitize_*)
# stub sqlalchemy/core.database at module scope with `if mod not in sys.modules`,
# which fires during collection. If the real module hasn't been imported yet,
# the stub wins and contaminates every subsequent test that needs the real ORM.
try:
    import sqlalchemy  # noqa: F401
    import sqlalchemy.orm  # noqa: F401
    import core.database  # noqa: F401
    import src.database
except ImportError:
    pass  # not installed - the stubs below will handle it

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db

# Pre-import core.models before test_agent_loop.py's module-level stubs
# run (it replaces sys.modules['core.models'] with a MagicMock during
# collection, which breaks session import in subsequent tests).
import core.models  # noqa: E402

def pytest_configure(config):
    """Register the dynamic taxonomy ``sub_*`` markers before collection.

    The stable ``area_*`` markers are declared in ``pyproject.toml``. The
    per-file ``sub_*`` markers are derived from the test filenames here so that
    unknown-mark warnings still surface genuine typos outside the taxonomy. This
    only registers marker names; it imports no production module.
    """
    import pathlib
    from tests._taxonomy import discover_markers

    tests_dir = pathlib.Path(__file__).parent
    paths = list(tests_dir.rglob("test_*.py")) + list(tests_dir.rglob("*_test.py"))
    for marker_name in discover_markers(paths):
        if marker_name.startswith("sub_"):
            config.addinivalue_line("markers", f"{marker_name}: taxonomy sub-area marker")


def pytest_collection_modifyitems(config, items):
    """Tag each collected test with its taxonomy ``area_*`` and ``sub_*`` markers.

    Collection-time only: this adds markers and nothing else. It does not skip,
    reorder, or deselect tests, mutate fixtures or the environment, or import any
    production module. See ``tests/_taxonomy.py`` for the classification rules.
    """
    import pytest
    from tests._taxonomy import markers_for_path

    for item in items:
        path = getattr(item, "path", None) or item.fspath
        for marker_name in markers_for_path(path):
            item.add_marker(getattr(pytest.mark, marker_name))


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def _declared_tools_root(tmp_path_factory):
    return str(tmp_path_factory.mktemp("declared_tools"))


_declared_tools_numbers = itertools.count()


@pytest.fixture(autouse=True)
def _isolated_declared_tools(_declared_tools_root):
    """Give every test an empty declared-tools state (src/stable_tools.py).

    The ChatGPT route persists each chat's declared tool list under the data
    folder; tests reuse session ids, so without this one test's list would be
    loaded by the next and the tools it sends would depend on test order.

    Each test gets its own folder name under one session folder. stable_tools
    creates the folder on its first write, so most tests never touch the disk;
    requesting ``tmp_path`` here made a numbered folder for every test, which
    cost about 30 s per serial run.

    Patched by hand, not with ``monkeypatch``: an autouse fixture that requests
    it makes monkeypatch outlive the test module's own fixtures, and a module
    fixture that reloads a module on teardown (test_upload_limits_centralized)
    then ran while the test's environment variables were still set.
    """
    mod = sys.modules.get("src.stable_tools")
    if mod is None:
        import src.stable_tools as mod
    mod.reset_for_tests()
    store = os.path.join(_declared_tools_root, str(next(_declared_tools_numbers)))
    original = mod._store_dir
    mod._store_dir = lambda: store
    try:
        yield
    finally:
        mod._store_dir = original
        mod.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_search_resilience_state():
    """Give every test fresh search gates, breakers and caches.

    ``services.search.resilience`` keeps process-wide state (per-provider
    concurrency gates, circuit breakers, a short-TTL result cache and a
    negative fetch cache). Without a reset, one test's mocked failure would
    trip a breaker or be served from a cache in the next test. Pacing sleeps
    are disabled; tests of the pacing itself install their own clock.
    """
    mod = sys.modules.get("services.search.resilience")
    real_sleep = None
    if mod is not None:
        real_sleep = mod._sleep
        mod.reset_state()
        mod._sleep = lambda _seconds: None
    try:
        yield
    finally:
        if mod is not None:
            mod._sleep = real_sleep
        # The module may have been imported during the test itself.
        late = sys.modules.get("services.search.resilience")
        if late is not None:
            late.reset_state()


@pytest.fixture(autouse=True)
def _isolate_integration_skill_registrations():
    """Restore src.builtin_skills' registered integration skill dirs after each test.

    App start-up (src/app_initializer.py) registers every integration package's
    skills in a module-level dict. Since 2026-10-02 several tests run start-up,
    and in CI's serial order the next seeding test then installed Todoist and
    Claude Code skills it never registered.
    """
    mod = sys.modules.get("src.builtin_skills")
    saved = dict(mod._integration_skill_dirs) if mod is not None else None
    try:
        yield
    finally:
        late = sys.modules.get("src.builtin_skills")
        if late is not None:
            late._integration_skill_dirs.clear()
            if saved is not None and late is mod:
                late._integration_skill_dirs.update(saved)


@pytest.fixture(autouse=True)
def _clear_embedding_and_cache_key_memory():
    """Process-level memos (query-embedding LRU, per-session prompt_cache_key)
    must not carry one test's fake embedder output or session keys into the next."""
    try:
        from src.embedding_lanes import clear_encode_cache
        clear_encode_cache()
        from src import llm_core
        llm_core._SESSION_CACHE_KEYS.clear()
    except Exception:
        pass
    yield
