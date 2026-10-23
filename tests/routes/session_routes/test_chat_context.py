"""GET /api/session/{id}/settings tells the context strip who answers, about what, and with what access."""
import pytest

import src.database
from core.database import Document


def _chat(client):
    return client.post("/api/session", data={"name": "c", "skip_validation": "true"}).json()["id"]


def _context(client, chat):
    response = client.get(f"/api/session/{chat}/settings")
    assert response.status_code == 200, response.text
    return response.json()["context"]


def _add_document(doc_id, title, owner, chat):
    db = src.database.SessionLocal()
    try:
        db.add(Document(id=doc_id, title=title, current_content="x", owner=owner, session_id=chat, is_active=True))
        db.commit()
    finally:
        db.close()


def test_a_default_chat_has_no_project_document_or_named_agent(api):
    alice = api.as_user("alice")
    context = _context(alice, _chat(alice))

    assert context["who"]["kind"] == "default"
    assert context["project"] is None
    assert context["document"] is None
    assert context["doc_target"] == "general"
    assert context["access"]["summary"] == "File editing allowed · Actions run without asking"


def test_a_chat_under_an_agent_names_it(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"agent_profile": "sds", "agent_persona_name": "SDS Consulting Lead"})

    assert _context(alice, chat)["who"] == {"kind": "agent", "name": "SDS Consulting Lead"}


def test_a_chat_with_a_prompt_preset_reports_a_persona(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"preset_id": "code_analyze"})

    assert _context(alice, chat)["who"]["kind"] == "persona"


@pytest.mark.parametrize("mode,summary", [
    ("auto", "File editing allowed · Actions run without asking"),
    ("ask_risky", "File editing allowed · External actions require approval"),
    ("ask_all", "Every change requires approval"),
])
def test_the_access_line_follows_the_approval_mode(api, mode, summary):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"approval_mode": mode})

    assert _context(alice, chat)["access"]["summary"] == summary


def test_a_chat_with_no_tools_says_so(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"tool_access": "none"})

    access = _context(alice, chat)["access"]
    assert access["summary"] == "No tools"
    assert access["files"] == "none"


def test_a_chat_in_chat_mode_makes_no_file_changes(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"toggles": {"mode": "chat"}})

    assert _context(alice, chat)["access"]["summary"] == "Chat only · no file changes"


def test_the_project_is_the_folder_after_a_projects_segment(api, tmp_path):
    admin = api.as_admin()
    chat = _chat(admin)
    folder = tmp_path / "AI Mind" / "Projects" / "Tall Tree Group" / "Agreements"
    folder.mkdir(parents=True)
    admin.patch(f"/api/session/{chat}/settings", json={"workspace": str(folder)})

    assert _context(admin, chat)["project"] == "Tall Tree Group"


def test_a_workspace_outside_projects_is_named_by_its_folder(api, tmp_path):
    admin = api.as_admin()
    chat = _chat(admin)
    folder = tmp_path / "checkout"
    folder.mkdir()
    admin.patch(f"/api/session/{chat}/settings", json={"workspace": str(folder)})

    assert _context(admin, chat)["project"] == "checkout"


def test_with_no_workspace_the_sidebar_folder_is_the_project(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    db = src.database.SessionLocal()
    try:
        db.query(src.database.Session).filter_by(id=chat).update({"folder": "Tall Tree Group"})
        db.commit()
    finally:
        db.close()

    assert _context(alice, chat)["project"] == "Tall Tree Group"


def test_the_document_is_the_chats_active_one_and_the_saved_target_is_reported(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    _add_document("d1", "DA-02", "alice", chat)
    alice.patch(f"/api/session/{chat}/settings", json={"doc_target": "project"})

    context = _context(alice, chat)

    assert context["document"] == {"id": "d1", "title": "DA-02"}
    assert context["doc_target"] == "project"


@pytest.mark.security
def test_another_users_chat_has_no_context_to_read(api):
    alice = api.as_user("alice")
    chat = _chat(alice)

    assert api.as_user("bob").get(f"/api/session/{chat}/settings").status_code in (403, 404)


@pytest.mark.security
def test_a_document_of_another_owner_never_shows_in_the_context(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    _add_document("d-bob", "BOB SECRET", "bob", chat)

    assert _context(alice, chat)["document"] is None
