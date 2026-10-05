"""Regression tests for endpoint owner scoping in secondary model routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.security


def _compare_request(user="alice", is_admin=False):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(is_admin=lambda u: is_admin)
            )
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_compare_router(monkeypatch):
    # setup_compare_routes registers on a module-global router. Give each test
    # its own, so the handlers bound to these fake session stores never end up
    # in an app that a later test in the same process imports.
    from fastapi import APIRouter
    from routes.compare import compare_routes

    monkeypatch.setattr(compare_routes, "router", APIRouter(prefix="/api/compare", tags=["compare"]))


def _compare_start_route(session_manager):
    from routes.compare_routes import setup_compare_routes

    router = setup_compare_routes(session_manager)
    # setup_compare_routes registers on a module-global router, so each call
    # appends another /start route; take the most recently registered one so we
    # get the handler bound to *this* session_manager.
    return [
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/api/compare/start"
    ][-1]


class _FakeDB:
    """The endpoint lookup is patched, so only the trailing Comparison insert
    touches this — swallow add/commit/close so the test never hits a real DB."""

    def add(self, *a, **k):
        pass

    def commit(self):
        pass

    def close(self):
        pass


class _SessionStore:
    def __init__(self, store):
        self._store = store

    def get(self, key, default=None):
        return self._store.get(key, default)


def test_compare_start_rejects_unregistered_endpoint_for_non_admin(monkeypatch):
    import routes.compare_routes as cr

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())
    # Nothing visible to the caller matches the supplied URL → raw, unregistered.
    monkeypatch.setattr(cr, "_owned_endpoint_by_url", lambda *a, **k: None)

    start = _compare_start_route(
        SimpleNamespace(create_session=lambda **_: None, sessions={})
    )
    with pytest.raises(HTTPException) as exc:
        start(
            _compare_request(),
            prompt="p",
            model_a="a",
            model_b="b",
            endpoint_a="http://127.0.0.1:8000/v1",
            endpoint_b="http://127.0.0.1:8001/v1",
        )

    assert exc.value.status_code == 403


def test_compare_start_allows_owned_registered_endpoint_for_non_admin(monkeypatch):
    # Regression: the followup must not blanket-reject non-admins. Compare
    # resolves endpoints by URL (no endpoint_id), so a caller comparing a
    # registered endpoint they own has to be allowed — only truly raw,
    # unregistered URLs are rejected.
    import routes.compare_routes as cr

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())
    owned = SimpleNamespace(id=7, api_key="sk-secret", base_url="http://127.0.0.1:8000/v1")
    monkeypatch.setattr(cr, "_owned_endpoint_by_url", lambda *a, **k: owned)

    created = {}

    def _create_session(session_id, **_):
        created[session_id] = SimpleNamespace(headers={})

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    # Must complete without raising 403.
    start(
        _compare_request(),
        prompt="p",
        model_a="a",
        model_b="b",
        endpoint_a="http://127.0.0.1:8000/v1",
        endpoint_b="http://127.0.0.1:8000/v1",
    )

    # Both [CMP] sessions created, each with the owned endpoint's key copied in.
    assert len(created) == 2
    for s in created.values():
        assert s.headers


def test_compare_start_rejects_another_users_private_endpoint(monkeypatch):
    # bob owns the endpoint at this URL; alice supplying the same URL gets no
    # match from the owner-scoped lookup (owner_filter drops bob's private row),
    # so compare treats it exactly like a raw unregistered URL → 403. She can
    # neither bind a session to his endpoint nor copy his key.
    import routes.compare_routes as cr

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())

    def _scoped(db, base, owner):
        # Only the owner ("bob") can see this private row; everyone else → None.
        if owner == "bob":
            return SimpleNamespace(id=9, api_key="sk-bob", base_url=base)
        return None

    monkeypatch.setattr(cr, "_owned_endpoint_by_url", _scoped)

    created = {}

    def _create_session(session_id, **_):
        created[session_id] = SimpleNamespace(headers={})

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    with pytest.raises(HTTPException) as exc:
        start(
            _compare_request(user="alice"),
            prompt="p",
            model_a="a",
            model_b="b",
            endpoint_a="http://10.0.0.5:9000/v1",
            endpoint_b="http://10.0.0.5:9000/v1",
        )

    assert exc.value.status_code == 403
    # Nothing was created → no session bound to bob's endpoint, no key copied.
    assert created == {}


def test_compare_start_rejects_before_creating_any_session_on_mixed_endpoints(monkeypatch):
    # Mixed request: endpoint A is a registered endpoint the caller owns,
    # endpoint B is a raw/unregistered URL. Both endpoints are resolved and
    # validated up front, so the unregistered B makes the WHOLE request 403 with
    # nothing created — no half-built [CMP] session for A, and therefore none of
    # A's Authorization header left behind. Fails on the old interleaved loop
    # that created A's session before reaching (and rejecting) B.
    import routes.compare_routes as cr
    from src.endpoint_resolver import normalize_base

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())
    owned = SimpleNamespace(id=7, api_key="sk-secret", base_url="http://127.0.0.1:8000/v1")
    owned_base = normalize_base(owned.base_url)

    def _scoped(db, base, owner):
        # Only endpoint A's URL maps to a visible registered endpoint; B → None.
        return owned if base == owned_base else None

    monkeypatch.setattr(cr, "_owned_endpoint_by_url", _scoped)

    created = {}

    def _create_session(session_id, **kw):
        created[session_id] = SimpleNamespace(headers={})

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    with pytest.raises(HTTPException) as exc:
        start(
            _compare_request(),
            prompt="p",
            model_a="a",
            model_b="b",
            endpoint_a="http://127.0.0.1:8000/v1",     # owned, registered
            endpoint_b="http://203.0.113.9:9999/v1",   # raw, unregistered
        )

    assert exc.value.status_code == 403
    # No partial session survives the reject, so no copied header does either.
    assert created == {}


def test_compare_start_binds_session_to_registered_endpoint_url(monkeypatch):
    # The session must dial the registered endpoint's OWN normalized base URL,
    # never the raw caller-supplied string. Mint the owned row with a base URL
    # that differs from the messy raw input so a regression to `endpoint_url=
    # endpoint` would surface here.
    import routes.compare_routes as cr
    from src.endpoint_resolver import build_chat_url, normalize_base

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())
    owned = SimpleNamespace(id=7, api_key="sk-secret", base_url="http://127.0.0.1:8000/v1")
    monkeypatch.setattr(cr, "_owned_endpoint_by_url", lambda *a, **k: owned)

    created = {}
    captured = {}

    def _create_session(session_id, **kw):
        created[session_id] = SimpleNamespace(headers={})
        captured[session_id] = kw

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    raw_url = "http://127.0.0.1:8000/v1/"  # trailing slash → not byte-identical
    start(
        _compare_request(),
        prompt="p",
        model_a="a",
        model_b="b",
        endpoint_a=raw_url,
        endpoint_b=raw_url,
    )

    expected = build_chat_url(normalize_base(owned.base_url))
    assert captured and all(kw["endpoint_url"] == expected for kw in captured.values())
    # The owned endpoint's key is copied into each session's headers.
    for s in created.values():
        assert s.headers


def test_compare_start_admin_raw_endpoint_carries_no_borrowed_key(monkeypatch):
    # Explicit admin/raw-endpoint behavior: an admin may pass a raw URL that
    # matches no registered endpoint. It is allowed (the reject helper is a
    # no-op for admins), the session keeps the raw URL, and — because nothing
    # matched — no key/headers are inherited from any endpoint row.
    import routes.compare_routes as cr

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(cr, "_owned_endpoint_by_url", lambda *a, **k: None)

    created = {}
    captured = {}

    def _create_session(session_id, **kw):
        created[session_id] = SimpleNamespace(headers={})
        captured[session_id] = kw

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    raw_url = "http://198.51.100.7:1234/v1"
    start(
        _compare_request(user="root", is_admin=True),
        prompt="p",
        model_a="a",
        model_b="b",
        endpoint_a=raw_url,
        endpoint_b=raw_url,
    )

    assert len(created) == 2
    for kw in captured.values():
        assert kw["endpoint_url"] == raw_url  # raw URL preserved for admins
    for s in created.values():
        assert s.headers == {}  # no borrowed key/headers


def test_compare_start_prefers_endpoint_id_over_url(monkeypatch):
    # Two endpoints visible to the caller share a base_url but hold DIFFERENT
    # api_keys (e.g. two accounts on one provider). A base_url-only match returns
    # whichever row sorts first, so it can copy the WRONG key. Passing the
    # explicit id must pin the intended endpoint and copy ITS key.
    import routes.compare_routes as cr
    from src.endpoint_resolver import build_chat_url, build_headers, normalize_base

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())

    url = "http://127.0.0.1:8000/v1"
    by_url = SimpleNamespace(id=1, api_key="sk-first", base_url=url)   # URL match
    by_id = SimpleNamespace(id=2, api_key="sk-second", base_url=url)   # id match

    # URL resolution would return the WRONG row; the id resolves the intended one.
    monkeypatch.setattr(cr, "_owned_endpoint_by_url", lambda *a, **k: by_url)
    monkeypatch.setattr(
        cr, "_owned_endpoint_by_id", lambda db, eid, owner: by_id if eid == "2" else None
    )

    created = {}
    captured = {}

    def _create_session(session_id, **kw):
        created[session_id] = SimpleNamespace(headers={})
        captured[session_id] = kw

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    start(
        _compare_request(),
        prompt="p",
        model_a="a",
        model_b="b",
        endpoint_a="",
        endpoint_b="",
        endpoint_a_id="2",
        endpoint_b_id="2",
    )

    expected_url = build_chat_url(normalize_base(url))
    expected_headers = build_headers("sk-second", url)
    assert captured and all(kw["endpoint_url"] == expected_url for kw in captured.values())
    # The id's key is copied in, NOT the same-URL row's key.
    for s in created.values():
        assert s.headers == expected_headers


def test_compare_start_rejects_unowned_endpoint_id(monkeypatch):
    # An id the caller can't see (wrong owner / deleted) must 404 and must NOT
    # silently fall back to a same-URL row with a different key.
    import routes.compare_routes as cr

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDB())
    # A same-URL row exists and would resolve, but the governing id is invisible.
    monkeypatch.setattr(
        cr,
        "_owned_endpoint_by_url",
        lambda *a, **k: SimpleNamespace(id=1, api_key="sk", base_url="http://127.0.0.1:8000/v1"),
    )
    monkeypatch.setattr(cr, "_owned_endpoint_by_id", lambda *a, **k: None)

    created = {}

    def _create_session(session_id, **_):
        created[session_id] = SimpleNamespace(headers={})

    start = _compare_start_route(
        SimpleNamespace(create_session=_create_session, sessions=_SessionStore(created))
    )
    with pytest.raises(HTTPException) as exc:
        start(
            _compare_request(),
            prompt="p",
            model_a="a",
            model_b="b",
            endpoint_a="",
            endpoint_b="",
            endpoint_a_id="999",
            endpoint_b_id="999",
        )

    assert exc.value.status_code == 404
    assert created == {}
