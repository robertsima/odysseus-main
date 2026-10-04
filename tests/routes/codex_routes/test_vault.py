"""The vault routes an external agent session reads the user's notes through.

An agent session sends what it retrieves to a hosted provider, so private
notes need a second scope, and the document route must not read files the
vault indexer does not track.
"""
from types import SimpleNamespace

import pytest

import src.rag_singleton
from src import ai_interaction

pytestmark = pytest.mark.security


def _bearer(api, scopes):
    response = api.as_admin().post("/api/tokens", data={"name": "agent", "scopes": scopes})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    notes = tmp_path / "vault"
    notes.mkdir()
    public = notes / "plan.md"
    public.write_text("public plan", encoding="utf-8")
    private = notes / "diary.md"
    private.write_text("private diary", encoding="utf-8")
    stray = tmp_path / "secret.txt"
    stray.write_text("not in the vault", encoding="utf-8")
    manager = SimpleNamespace(index=[
        {"path": str(public), "sensitivity": "public"},
        {"path": str(private), "sensitivity": "private"},
    ])
    monkeypatch.setattr(ai_interaction, "_personal_docs_manager", manager, raising=False)
    return SimpleNamespace(notes=notes, public=public, private=private, stray=stray)


def _read(api, headers, path):
    return api.anonymous().get("/api/codex/vault/document", params={"path": str(path)}, headers=headers)


def test_an_indexed_note_is_read(api, vault):
    response = _read(api, _bearer(api, "vault:read"), vault.public)

    assert response.status_code == 200
    assert response.json()["content"] == "public plan"


@pytest.mark.parametrize("which", ["stray", "traversal"])
def test_a_file_the_vault_does_not_index_cannot_be_read(api, vault, which):
    path = vault.stray if which == "stray" else f"{vault.notes}/../secret.txt"

    response = _read(api, _bearer(api, "vault:read"), path)

    assert response.status_code == 404
    assert "not in the vault" not in response.text


def test_a_private_note_needs_the_private_scope(api, vault):
    refused = _read(api, _bearer(api, "vault:read"), vault.private)
    allowed = _read(api, _bearer(api, "vault:read,vault:read_private"), vault.private)

    assert refused.status_code == 403
    assert "private diary" not in refused.text
    assert allowed.json()["content"] == "private diary"


@pytest.fixture
def vector_store(monkeypatch):
    searches = []

    def search(query, k, allow_private):
        searches.append({"query": query, "allow_private": allow_private})
        return [{"document": "x" * 10_000, "similarity": 0.9,
                 "metadata": {"file_path": "/vault/plan.md", "title": "Plan"}}]

    store = SimpleNamespace(healthy=True, search=search, searches=searches)
    monkeypatch.setattr(src.rag_singleton, "get_rag_manager", lambda: store)
    return store


def _search(api, headers, **params):
    return api.anonymous().get("/api/codex/vault/search", params={"q": "plans", **params}, headers=headers)


def test_search_returns_a_bounded_excerpt_not_the_whole_note(api, vector_store):
    response = _search(api, _bearer(api, "vault:read"))

    hit = response.json()["results"][0]
    assert len(hit["excerpt"]) <= 2000
    assert hit["truncated"] is True
    assert hit["path"] == "/vault/plan.md"


def test_search_leaves_private_notes_out_unless_asked(api, vector_store):
    _search(api, _bearer(api, "vault:read,vault:read_private"))

    assert vector_store.searches[-1]["allow_private"] is False


def test_a_private_search_needs_the_private_scope(api, vector_store):
    refused = _search(api, _bearer(api, "vault:read"), include_private="true")
    allowed = _search(api, _bearer(api, "vault:read,vault:read_private"), include_private="true")

    assert refused.status_code == 403
    assert [s["allow_private"] for s in vector_store.searches] == [True]
    assert allowed.json()["include_private"] is True


def test_capabilities_tell_the_agent_which_vault_access_its_token_has(api):
    response = api.anonymous().get("/api/codex/capabilities", headers=_bearer(api, "vault:read"))

    vault = response.json()["tools"]["vault"]
    assert vault["read"] is True
    assert vault["read_private"] is False
