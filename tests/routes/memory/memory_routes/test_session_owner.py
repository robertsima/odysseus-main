"""Memory routes must owner-scope a caller-supplied session id.

SessionManager.get_session returns any session by id. /api/memory/add and
/api/memory/import accept one, so without an ownership gate a user could file
a memory under another tenant's session or run an import through another
tenant's LLM credentials.
"""
import pytest

import routes.memory.memory_routes as memory_routes
import src.database

pytestmark = pytest.mark.security

UTILITY = ("http://utility.test/v1/chat/completions", "utility-model", {"Authorization": "Bearer utility"})
UPLOAD = {"file": ("notes.txt", b"Alice leads Project Phoenix.", "text/plain")}


def _session(api, owner, **attrs):
    created = api.as_user(owner).post("/api/session", data={"name": f"{owner}'s", "skip_validation": "true"})
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    if attrs:
        # The manager refreshes a cached session from its row, so set the row.
        db = src.database.SessionLocal()
        try:
            row = db.query(src.database.Session).filter(src.database.Session.id == sid).one()
            for key, value in attrs.items():
                setattr(row, key, value)
            db.commit()
        finally:
            db.close()
    return sid


@pytest.fixture
def model_calls(monkeypatch):
    """The model boundary: record where an import's extraction call is sent."""
    calls = []

    async def llm_call_async(url, model, messages, **kwargs):
        calls.append({"url": url, "model": model, "headers": kwargs.get("headers")})
        return '[{"text": "Alice leads Project Phoenix", "category": "project"}]'

    monkeypatch.setattr(memory_routes, "llm_call_async", llm_call_async)
    monkeypatch.setattr(memory_routes, "resolve_endpoint", lambda role, owner=None: UTILITY)
    return calls


def _memories(client):
    return client.get("/api/memory").json()["memory"]


def test_a_memory_cannot_be_filed_under_another_users_session(api):
    bob_session = _session(api, "bob")
    alice = api.as_user("alice")
    before = _memories(alice)

    response = alice.post("/api/memory/add", json={"text": "Alice note", "session_id": bob_session})

    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"
    assert _memories(alice) == before


def test_a_memory_can_be_filed_under_your_own_session(api):
    alice_session = _session(api, "alice")
    alice = api.as_user("alice")

    response = alice.post("/api/memory/add", json={"text": "Alice note", "session_id": alice_session})

    assert response.status_code == 200, response.text
    assert alice_session in [m.get("session_id") for m in _memories(alice)]


def test_an_import_naming_another_users_session_does_not_use_its_credentials(api, model_calls):
    bob_session = _session(
        api, "bob", endpoint_url="http://bob-llm/v1/chat/completions", model="bob-model",
        headers={"Authorization": "Bearer bob-secret"},
    )

    response = api.as_user("alice").post("/api/memory/import", data={"session": bob_session}, files=UPLOAD)

    assert response.status_code == 200, response.text
    assert [s["text"] for s in response.json()["suggestions"]] == ["Alice leads Project Phoenix"]
    assert [(c["url"], c["model"]) for c in model_calls] == [UTILITY[:2]]
    assert "bob-secret" not in str(model_calls)


def test_an_import_naming_a_missing_session_falls_back_to_the_utility_model(api, model_calls):
    response = api.as_user("alice").post("/api/memory/import", data={"session": "no-such-session"}, files=UPLOAD)

    assert response.status_code == 200, response.text
    assert [(c["url"], c["model"]) for c in model_calls] == [UTILITY[:2]]


def test_an_import_naming_your_own_session_uses_that_sessions_model(api, model_calls):
    alice_session = _session(
        api, "alice", endpoint_url="http://alice-llm/v1/chat/completions", model="alice-model",
        headers={"X-Session": "alice"},
    )

    response = api.as_user("alice").post("/api/memory/import", data={"session": alice_session}, files=UPLOAD)

    assert response.status_code == 200, response.text
    assert [(c["url"], c["model"]) for c in model_calls] == [
        ("http://alice-llm/v1/chat/completions", "alice-model")
    ]
    assert model_calls[0]["headers"]["X-Session"] == "alice"
