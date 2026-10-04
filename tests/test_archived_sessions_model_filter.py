"""Archive browser model filter must be a CONTAINS match, not suffix-only.

list_archived_sessions filtered with DbSession.model.ilike(f"%{model}") - a
suffix match. Filtering by "gpt-4" therefore returned "openai/gpt-4" but
silently DROPPED "gpt-4o" (contains but does not end with the value), and
over-matched models that merely share the suffix. The sibling name filter
already uses a wildcard-escaped contains match.
"""
import sys
import types
import uuid

import pytest

from core.database import Session as DbSession


def _route(router, path, method="GET"):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {path}")


def _stub_multipart_if_missing(monkeypatch):
    """Satisfy FastAPI's optional python-multipart probe.

    setup_session_routes() registers form-based routes we don't exercise here.
    When FastAPI analyzes their Form() params at registration time it calls
    ensure_multipart_is_installed(), which raises RuntimeError if neither
    python-multipart nor multipart is importable. This archived-session model
    filter test must not depend on that optional package, so inject a minimal
    stub (only when it's genuinely absent) to let route setup proceed.
    """
    try:
        import python_multipart  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("python_multipart")
    stub.__version__ = "0.0.20"  # FastAPI asserts __version__ > "0.0.12"
    monkeypatch.setitem(sys.modules, "python_multipart", stub)


@pytest.fixture
def archived_endpoint(monkeypatch, app_db):
    import routes.session_routes as sr
    from unittest.mock import MagicMock

    _stub_multipart_if_missing(monkeypatch)
    monkeypatch.setattr(sr, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")
    router = sr.setup_session_routes(MagicMock(), {})
    return _route(router, "/api/sessions/archived")


def _seed(app_db, owner, *models):
    db = app_db.SessionLocal()
    try:
        for m in models:
            db.add(DbSession(id=str(uuid.uuid4()), owner=owner, name=f"chat {m}",
                             endpoint_url="http://localhost", model=m, archived=True))
        db.commit()
    finally:
        db.close()


def test_contains_match_returns_all_models_sharing_the_substring(archived_endpoint, app_db):
    _seed(app_db, "alice", "openai/gpt-4", "gpt-4o", "claude-3")
    res = archived_endpoint(request=None, model="gpt-4")
    got = {s["model"] for s in res["sessions"]}
    assert got == {"openai/gpt-4", "gpt-4o"}


def test_exact_full_model_still_matches(archived_endpoint, app_db):
    _seed(app_db, "alice", "openai/gpt-4", "gpt-4o")
    res = archived_endpoint(request=None, model="openai/gpt-4")
    assert {s["model"] for s in res["sessions"]} == {"openai/gpt-4"}


def test_wildcard_in_filter_is_escaped(archived_endpoint, app_db):
    _seed(app_db, "alice", "gpt-4o", "gpt_4o")
    res = archived_endpoint(request=None, model="gpt_4")
    assert {s["model"] for s in res["sessions"]} == {"gpt_4o"}
