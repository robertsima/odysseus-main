"""Edit, regenerate and retry delete chat messages by stored id, not position.

The chat renders only the newest page of history (24 bubbles on desktop), and
the UI used to send a bubble's position in that page as ``keep_count``. On
2026-09-29 editing the 22nd bubble of a 433-message chat kept the first 22
messages and deleted the other 411. These tests pin the id-based paths:
``from_message_id`` / ``after_message_id`` for truncate, ``through_message_id``
for fork, and ``keep_last`` for ``/truncate N`` and the agent's
manage_session truncate (both promise to keep the newest N).
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import QueuePool

from core.models import ChatMessage


@pytest.fixture
def managed(monkeypatch, make_test_db):
    """A SessionManager on its own SQLite file. Patched, not reloaded: a
    reloaded core.database leaks into every test that runs after this one."""
    import core.database as database
    import core.session_manager as sm_mod

    factory = make_test_db(poolclass=QueuePool).SessionLocal
    monkeypatch.setattr(sm_mod, "SessionLocal", factory)
    db = SimpleNamespace(SessionLocal=factory, ChatMessage=database.ChatMessage, Session=database.Session)
    return sm_mod.SessionManager(), db


def _chat(sm, sid="chat", n=30):
    sm.create_session(session_id=sid, name="t", endpoint_url="x", model="m", rag=False, owner="u")
    for i in range(n):
        sm.add_message(sid, ChatMessage("user" if i % 2 == 0 else "assistant", f"msg{i}"))
    return [m.metadata["_db_id"] for m in sm.sessions[sid].history]


def _stored(database, sid="chat"):
    db = database.SessionLocal()
    try:
        rows = db.query(database.ChatMessage).filter(
            database.ChatMessage.session_id == sid
        ).order_by(database.ChatMessage.timestamp).all()
        count = db.query(database.Session).filter(database.Session.id == sid).first().message_count
        return [r.content for r in rows], count
    finally:
        db.close()


def test_truncate_from_message_deletes_only_that_message_and_later(managed):
    sm, database = managed
    ids = _chat(sm)

    # The 22nd bubble of the rendered page is message 28 of this 30-message chat.
    assert sm.truncate_from_message("chat", ids[28]) == 2

    contents, count = _stored(database)
    assert contents == [f"msg{i}" for i in range(28)]
    assert count == 28
    assert [m.content for m in sm.sessions["chat"].history] == contents


def test_truncate_after_message_keeps_the_anchor(managed):
    sm, database = managed
    ids = _chat(sm)

    assert sm.truncate_from_message("chat", ids[27], inclusive=False) == 2

    contents, count = _stored(database)
    assert contents == [f"msg{i}" for i in range(28)]
    assert count == 28
    assert len(sm.sessions["chat"].history) == 28


def test_truncate_from_unknown_message_deletes_nothing(managed):
    sm, database = managed
    _chat(sm)

    assert sm.truncate_from_message("chat", "not-a-message") is None
    assert sm.truncate_from_message("chat", "") is None

    contents, count = _stored(database)
    assert len(contents) == 30
    assert count == 30


def test_keep_last_messages_keeps_the_newest(managed):
    sm, database = managed
    _chat(sm)

    assert sm.keep_last_messages("chat", 5) == 25

    contents, count = _stored(database)
    assert contents == [f"msg{i}" for i in range(25, 30)]
    assert count == 5
    assert [m.content for m in sm.sessions["chat"].history] == contents


def test_keep_last_more_than_exist_deletes_nothing(managed):
    sm, database = managed
    _chat(sm, n=3)

    assert sm.keep_last_messages("chat", 10) == 0
    contents, count = _stored(database)
    assert len(contents) == 3 and count == 3


# ── Endpoints ──────────────────────────────────────────────────────────── #

def _handler(router, suffix):
    for route in router.routes:
        if getattr(route, "path", "").endswith(suffix) and "POST" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{suffix} route not found")


def _request(body):
    async def _json():
        return body
    return SimpleNamespace(json=_json)


@pytest.fixture
def routes(monkeypatch, managed):
    import routes.history.history_routes as mod
    monkeypatch.setattr(mod, "_verify_session_owner", lambda *a, **k: None)
    sm, database = managed
    # The route module bound these at import, before the temp database.
    monkeypatch.setattr(mod, "SessionLocal", database.SessionLocal)
    monkeypatch.setattr(mod, "DbSession", database.Session)
    monkeypatch.setattr(mod, "DbChatMessage", database.ChatMessage)
    # Session.add_message persists through the process-wide manager.
    import core.models as models
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", sm)
    ids = _chat(sm)
    router = mod.setup_history_routes(sm)
    return SimpleNamespace(sm=sm, database=database, ids=ids, router=router)


def test_truncate_endpoint_by_message_id(routes):
    truncate = _handler(routes.router, "/truncate")
    result = asyncio.run(truncate(request=_request({"from_message_id": routes.ids[20]}), session_id="chat"))
    assert result == {"status": "ok", "deleted": 10, "truncated": True}
    assert _stored(routes.database)[0] == [f"msg{i}" for i in range(20)]


def test_truncate_endpoint_after_message_id(routes):
    truncate = _handler(routes.router, "/truncate")
    result = asyncio.run(truncate(request=_request({"after_message_id": routes.ids[20]}), session_id="chat"))
    assert result["deleted"] == 9
    assert _stored(routes.database)[0] == [f"msg{i}" for i in range(21)]


def test_truncate_endpoint_unknown_id_is_404_and_deletes_nothing(routes):
    truncate = _handler(routes.router, "/truncate")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(truncate(request=_request({"from_message_id": "gone"}), session_id="chat"))
    assert exc.value.status_code == 404
    assert len(_stored(routes.database)[0]) == 30


def test_truncate_endpoint_keep_last(routes):
    truncate = _handler(routes.router, "/truncate")
    result = asyncio.run(truncate(request=_request({"keep_last": 4}), session_id="chat"))
    assert result["deleted"] == 26
    assert _stored(routes.database)[0] == [f"msg{i}" for i in range(26, 30)]


def test_fork_through_message_id_copies_up_to_that_message(routes):
    fork = _handler(routes.router, "/fork")
    result = asyncio.run(fork(request=_request({"through_message_id": routes.ids[24]}), session_id="chat"))
    assert result["status"] == "ok"
    forked = routes.sm.get_session(result["id"])
    assert [m.content for m in forked.history] == [f"msg{i}" for i in range(25)]
    # The source chat is untouched.
    assert len(_stored(routes.database)[0]) == 30


def test_fork_through_unknown_message_is_404_and_creates_nothing(routes):
    fork = _handler(routes.router, "/fork")
    before = set(routes.sm.sessions)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fork(request=_request({"through_message_id": "gone"}), session_id="chat"))
    assert exc.value.status_code == 404
    assert set(routes.sm.sessions) == before


def test_paged_history_carries_each_rows_own_id(routes):
    # The chat loads pages through this path; without the id a reloaded bubble
    # has nothing to edit, regenerate or fork by.
    get_history = next(
        r.endpoint for r in routes.router.routes
        if getattr(r, "path", "") == "/api/history/{session_id}" and "GET" in r.methods
    )
    page = asyncio.run(get_history(request=SimpleNamespace(), session_id="chat", limit=24, offset=None))
    assert len(page["history"]) == 24
    assert [e["metadata"]["_db_id"] for e in page["history"]] == routes.ids[6:]


def test_forked_rows_report_their_own_id_not_the_sources(routes):
    fork = _handler(routes.router, "/fork")
    result = asyncio.run(fork(request=_request({"through_message_id": routes.ids[3]}), session_id="chat"))
    get_history = next(
        r.endpoint for r in routes.router.routes
        if getattr(r, "path", "") == "/api/history/{session_id}" and "GET" in r.methods
    )
    page = asyncio.run(get_history(request=SimpleNamespace(), session_id=result["id"], limit=24, offset=None))
    ids = [e["metadata"]["_db_id"] for e in page["history"]]
    assert len(ids) == 4 and not set(ids) & set(routes.ids)


# ── Frontend call sites ────────────────────────────────────────────────── #

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_chat_ui_never_truncates_or_forks_by_position():
    js = _read("static/js/chat.js")
    assert "keep_count" not in js.replace("used to send that position as keep_count", "")
    assert "from_message_id" in js and "after_message_id" in js
    assert "through_message_id" in js
    # The user's bubble learns its stored id while the reply streams.
    assert "user_message_saved" in js


def test_slash_truncate_keeps_the_last_n():
    js = _read("static/js/slashCommands.js")
    start = js.index("async function _cmdSessionTruncate")
    body = js[start:js.index("\n}\n", start)]
    assert "keep_last" in body and "keep_count" not in body
