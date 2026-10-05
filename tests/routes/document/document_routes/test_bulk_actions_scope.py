"""The bulk document actions (zip export, tidy, AI tidy) act only on the caller's documents."""
import io
import json
import re
import zipfile
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.security

ALICE_TEXT = "Alice's diary entry"


def _create_doc(client, title, content):
    response = client.post("/api/document", json={"title": title, "content": content})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _backdate(doc_id, hours=2):
    """Make a document old enough for tidy to consider it (it skips fresh ones)."""
    import core.database as database

    db = database.SessionLocal()
    try:
        db.query(database.Document).filter(database.Document.id == doc_id).update(
            {"created_at": datetime.utcnow() - timedelta(hours=hours)}
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


def test_zip_export_skips_another_users_documents(alice, bob):
    alice_doc = _create_doc(alice, "diary", ALICE_TEXT)
    bob_doc = _create_doc(bob, "shopping", "eggs")

    only_alices = bob.post("/api/documents/export-zip", json={"ids": [alice_doc]})
    mixed = bob.post("/api/documents/export-zip", json={"ids": [alice_doc, bob_doc]})

    assert only_alices.status_code == 404
    with zipfile.ZipFile(io.BytesIO(mixed.content)) as zf:
        contents = [zf.read(name).decode() for name in zf.namelist()]
    assert contents == ["eggs"]


def test_the_owner_can_export_their_documents_as_a_zip(alice):
    alice_doc = _create_doc(alice, "diary", ALICE_TEXT)

    response = alice.post("/api/documents/export-zip", json={"ids": [alice_doc]})

    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert [zf.read(name).decode() for name in zf.namelist()] == [ALICE_TEXT]


def test_tidy_leaves_another_users_junk_alone_and_cleans_the_callers(alice, bob):
    alice_junk = _create_doc(alice, "Untitled", "")
    _backdate(alice_junk)

    bob.post("/api/documents/tidy")
    assert alice.get(f"/api/document/{alice_junk}").status_code == 200

    tidied = alice.post("/api/documents/tidy").json()
    assert tidied["deleted"] == 1
    assert alice.get(f"/api/document/{alice_junk}").status_code == 404


@pytest.fixture
def junk_model(monkeypatch):
    """A model that calls every document it is shown junk, and remembers what it saw."""
    import src.llm_core
    import src.task_endpoint

    prompts = []

    async def judge_everything_junk(url, model, messages, **kwargs):
        text = json.dumps(messages)
        prompts.append(text)
        return json.dumps(sorted(set(re.findall(r"\[(d\d+)\]", text))))

    monkeypatch.setattr(
        src.task_endpoint, "resolve_task_endpoint",
        lambda owner=None: ("http://127.0.0.1:9/v1/chat/completions", "junk-judge", {}),
    )
    monkeypatch.setattr(src.llm_core, "llm_call_async", judge_everything_junk)
    return prompts


def test_ai_tidy_never_shows_or_deletes_another_users_documents(alice, bob, junk_model):
    alice_doc = _create_doc(alice, "diary", ALICE_TEXT)
    bob_doc = _create_doc(bob, "scratch", "asdf asdf")

    result = bob.post("/api/documents/ai-tidy").json()

    assert (result["reviewed"], result["deleted"]) == (1, 1)
    assert ALICE_TEXT not in junk_model[0]
    assert bob.get(f"/api/document/{bob_doc}").status_code == 404
    assert alice.get(f"/api/document/{alice_doc}").json()["current_content"] == ALICE_TEXT
