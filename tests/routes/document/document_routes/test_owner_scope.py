"""A user reaches only their own documents, and another user's look missing.

Bob's request for one of Alice's documents must get the same answer as a
request for an id that does not exist, so the response can't confirm that the
id belongs to someone.
"""
import sys
import types
import uuid

import pytest

pytestmark = pytest.mark.security

ALICE_TITLE = "Alice's launch plan"
ALICE_TEXT = "Launch on Friday; tell nobody."


@pytest.fixture(autouse=True)
def _importable_pymupdf(monkeypatch):
    """ai-fill-annotations imports PyMuPDF before its owner check, and CI does
    not install it. No request here gets far enough to use it."""
    try:
        import fitz  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "fitz", types.ModuleType("fitz"))


def _create_doc(client, title, content, **extra):
    response = client.post("/api/document", json={"title": title, "content": content, **extra})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _create_session(client, name):
    response = client.post("/api/session", data={"name": name, "skip_validation": "true"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


@pytest.fixture
def alice_doc(alice):
    return _create_doc(alice, ALICE_TITLE, ALICE_TEXT)


def _missing_id():
    return str(uuid.uuid4())


READS = [
    ("GET", "/api/document/{id}", None),
    ("GET", "/api/document/{id}/versions", None),
    ("GET", "/api/document/{id}/version/1", None),
    ("POST", "/api/document/{id}/extract-pdf-text", None),
    ("POST", "/api/document/{id}/export-pdf/preview", None),
    ("GET", "/api/document/{id}/render-pages", None),
    ("GET", "/api/document/{id}/page/1.png", None),
    ("GET", "/api/document/{id}/render-pdf", None),
    ("GET", "/api/document/{id}/export-pdf", None),
    ("POST", "/api/document/{id}/prepare-signed-reply", None),
    ("POST", "/api/document/{id}/ai-fill-annotations", {"instruction": "fill in my name"}),
]

CHANGES = [
    ("PUT", "/api/document/{id}", {"content": "bob was here"}),
    ("PATCH", "/api/document/{id}", {"title": "bob's now"}),
    ("DELETE", "/api/document/{id}", None),
    ("POST", "/api/document/{id}/archive", None),
    ("POST", "/api/document/{id}/restore/1", None),
]


@pytest.mark.parametrize("method, path, body", READS + CHANGES)
def test_another_users_document_answers_like_a_missing_one(bob, alice_doc, method, path, body):
    on_alices = bob.request(method, path.format(id=alice_doc), json=body)
    on_missing = bob.request(method, path.format(id=_missing_id()), json=body)

    assert on_alices.status_code == 404
    assert (on_alices.status_code, on_alices.json()) == (on_missing.status_code, on_missing.json())
    assert ALICE_TEXT not in on_alices.text


@pytest.mark.parametrize("method, path, body", CHANGES)
def test_another_user_cannot_change_or_delete_a_document(alice, bob, alice_doc, method, path, body):
    bob.request(method, path.format(id=alice_doc), json=body)

    doc = alice.get(f"/api/document/{alice_doc}").json()
    assert (doc["title"], doc["current_content"]) == (ALICE_TITLE, ALICE_TEXT)
    assert (doc["is_active"], doc["archived"], doc["version_count"]) == (True, False, 1)


def test_the_library_lists_and_counts_only_the_callers_documents(alice, bob):
    alice_doc = _create_doc(alice, ALICE_TITLE, ALICE_TEXT, session_id=_create_session(alice, "a"))
    bob_doc = _create_doc(bob, "Bob's notes", "groceries", session_id=_create_session(bob, "b"))

    alice_lib = alice.get("/api/documents/library").json()
    bob_lib = bob.get("/api/documents/library").json()

    assert [d["id"] for d in alice_lib["documents"]] == [alice_doc]
    assert [d["id"] for d in bob_lib["documents"]] == [bob_doc]
    assert (bob_lib["total"], sum(bob_lib["languages"].values()), bob_lib["session_count"]) == (1, 1, 1)


def test_the_archived_view_lists_only_the_callers_documents(alice, bob, alice_doc):
    assert alice.post(f"/api/document/{alice_doc}/archive").json()["archived"] is True

    alice_archived = alice.get("/api/documents/library", params={"archived": "true"}).json()
    bob_archived = bob.get("/api/documents/library", params={"archived": "true"}).json()

    assert [d["id"] for d in alice_archived["documents"]] == [alice_doc]
    assert (bob_archived["documents"], bob_archived["total"]) == ([], 0)


def test_another_users_chat_documents_answer_like_a_missing_chat(alice, bob):
    session = _create_session(alice, "alice's chat")
    _create_doc(alice, ALICE_TITLE, ALICE_TEXT, session_id=session)

    on_alices = bob.get(f"/api/documents/{session}")
    on_missing = bob.get(f"/api/documents/{_missing_id()}")

    assert on_alices.status_code == 404
    assert on_alices.json() == on_missing.json()
    assert [d["title"] for d in alice.get(f"/api/documents/{session}").json()] == [ALICE_TITLE]


def test_a_user_cannot_create_a_document_in_another_users_chat(alice, bob):
    session = _create_session(alice, "alice's chat")

    response = bob.post("/api/document", json={"title": "planted", "content": "x", "session_id": session})

    assert response.status_code == 404
    assert alice.get(f"/api/documents/{session}").json() == []


def test_a_user_cannot_move_their_document_into_another_users_chat(alice, bob):
    session = _create_session(alice, "alice's chat")
    bob_doc = _create_doc(bob, "Bob's notes", "groceries")

    response = bob.patch(f"/api/document/{bob_doc}", json={"session_id": session})

    assert response.status_code == 404
    assert bob.get(f"/api/document/{bob_doc}").json()["session_id"] is None
    assert alice.get(f"/api/documents/{session}").json() == []


def test_a_user_cannot_import_a_pdf_into_another_users_chat(alice, bob):
    session = _create_session(alice, "alice's chat")

    response = bob.post(
        "/api/documents/import-pdf",
        data={"session_id": session},
        files={"file": ("form.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")},
    )

    assert response.status_code == 404
    assert alice.get(f"/api/documents/{session}").json() == []


def test_the_owner_can_read_edit_and_version_their_document(alice, alice_doc):
    assert alice.get(f"/api/document/{alice_doc}").json()["current_content"] == ALICE_TEXT

    edited = alice.put(f"/api/document/{alice_doc}", json={"content": "Launch on Monday.", "force_version": True})
    assert (edited.status_code, edited.json()["version_count"]) == (200, 2)

    versions = alice.get(f"/api/document/{alice_doc}/versions").json()
    assert [v["version_number"] for v in versions] == [2, 1]
    assert alice.get(f"/api/document/{alice_doc}/version/1").json()["content"] == ALICE_TEXT

    restored = alice.post(f"/api/document/{alice_doc}/restore/1").json()
    assert restored["current_content"] == ALICE_TEXT

    renamed = alice.patch(f"/api/document/{alice_doc}", json={"title": "Plan B"}).json()
    assert renamed["title"] == "Plan B"


def test_the_owner_can_archive_and_delete_their_document(alice, alice_doc):
    assert alice.post(f"/api/document/{alice_doc}/archive").json()["archived"] is True
    assert alice.post(f"/api/document/{alice_doc}/archive", params={"archived": "false"}).json()["archived"] is False

    assert alice.delete(f"/api/document/{alice_doc}").json() == {"status": "deleted", "id": alice_doc}
    assert alice.get("/api/documents/library").json()["documents"] == []


@pytest.mark.parametrize("method, path, body, refusal", [
    ("POST", "/api/document/{id}/extract-pdf-text", None, "Document is not a PDF"),
    ("POST", "/api/document/{id}/export-pdf/preview", None, "Document is not linked to a source PDF"),
    ("GET", "/api/document/{id}/render-pages", None, "Document is not linked to a source PDF"),
    ("GET", "/api/document/{id}/page/1.png", None, "Document is not linked to a source PDF"),
    ("GET", "/api/document/{id}/render-pdf", None, "Document is not linked to a source PDF"),
    ("GET", "/api/document/{id}/export-pdf", None, "Document is not linked to a source PDF"),
    ("POST", "/api/document/{id}/prepare-signed-reply", None, "Document has no source email"),
    ("POST", "/api/document/{id}/ai-fill-annotations", {"instruction": "fill"}, "Document is not linked to a source PDF"),
])
def test_the_owner_gets_past_the_owner_check_on_pdf_endpoints(alice, alice_doc, method, path, body, refusal):
    """A plain text document has no PDF, so the owner's request stops at that check, after the owner check."""
    response = alice.request(method, path.format(id=alice_doc), json=body)

    assert response.status_code == 400
    assert response.json()["detail"].startswith(refusal)
