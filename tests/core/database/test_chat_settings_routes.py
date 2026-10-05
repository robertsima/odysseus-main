"""Per-chat settings storage and fork lineage, against a throwaway SQLite database."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

import core.database as cdb
from core.database import Session as DbSession


@pytest.fixture
def db(monkeypatch, app_db):
    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    return app_db


def _add(db, sid, name="chat", **cols):
    s = db.SessionLocal()
    s.add(DbSession(id=sid, owner="alice", name=name, endpoint_url="http://x", model="m", archived=False, **cols))
    s.commit()
    s.close()


def test_settings_helpers_merge_and_remove(db):
    sid = str(uuid.uuid4())
    _add(db, sid)
    assert cdb.get_session_settings(sid) == {}
    assert cdb.update_session_settings(sid, {"approval_mode": "ask_risky", "workspace": "/w"}) == {
        "approval_mode": "ask_risky", "workspace": "/w"}
    assert cdb.update_session_settings(sid, {"workspace": None, "disabled_tools": ["bash"]}) == {
        "approval_mode": "ask_risky", "disabled_tools": ["bash"]}
    assert cdb.get_session_settings(sid)["disabled_tools"] == ["bash"]
    assert cdb.update_session_settings("missing", {"x": 1}) is None


def _req(body=None):
    async def _json():
        if isinstance(body, Exception):
            raise body
        return body
    return SimpleNamespace(json=_json)


def test_fork_records_its_source_copies_settings_and_defaults_to_everything(db, monkeypatch):
    import routes.history_routes as hr
    from core.models import ChatMessage

    monkeypatch.setattr(hr, "_verify_session_owner", lambda *a, **k: None)
    src_id = str(uuid.uuid4())
    _add(db, src_id, name="Original")
    cdb.update_session_settings(src_id, {"approval_mode": "ask_risky"})

    class Sess:
        def __init__(self, name):
            self.name, self.owner, self.endpoint_url, self.model, self.history = name, "alice", "http://x", "m", []

        def add_message(self, m):
            self.history.append(m)

    source = Sess("Original")
    source.history = [ChatMessage("user", "a"), ChatMessage("assistant", "b"), ChatMessage("user", "c")]
    made = {}

    class SM:
        sessions = {src_id: source}

        def get_session(self, session_id):
            # The fork route reads the source through get_session so a
            # metadata-only session is hydrated before its history is copied.
            return self.sessions[session_id]

        def create_session(self, session_id, name, endpoint_url, model, rag, owner):
            _add(db, session_id, name=name)
            made["s"] = Sess(name)
            return made["s"]

    fork = next(r.endpoint for r in hr.setup_history_routes(SM()).routes if r.path.endswith("/fork"))
    out = asyncio.run(fork(request=_req({}), session_id=src_id))
    assert out["kept"] == 3 and out["forked_from"] == src_id
    s = db.SessionLocal()
    row = s.query(DbSession).filter(DbSession.id == out["id"]).first()
    s.close()
    assert row.forked_from == src_id
    assert json.loads(row.settings_json)["approval_mode"] == "ask_risky"
