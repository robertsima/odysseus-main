"""A chat's saved workspace gets the same checks as one sent with a message.

The workspace picker now saves the folder on the chat (2026-10-09), and headless
workers read that saved value. Before, PATCH /settings stored any string, while
the chat route only binds a folder for an admin and only after vet_workspace.
"""
import os

import pytest

pytestmark = pytest.mark.security


def _new_chat(client):
    return client.post("/api/session", data={"name": "build", "skip_validation": "true"}).json()["id"]


def test_an_admin_saves_a_folder_in_its_canonical_form(api, tmp_path):
    admin = api.as_admin()
    chat = _new_chat(admin)
    folder = tmp_path / "project"
    folder.mkdir()

    response = admin.patch(f"/api/session/{chat}/settings", json={"workspace": str(folder / ".." / "project")})

    assert response.status_code == 200
    assert os.path.normcase(response.json()["settings"]["workspace"]) == os.path.normcase(os.path.realpath(folder))


def test_a_regular_user_cannot_bind_a_folder(api, tmp_path):
    alice = api.as_user("alice")
    chat = _new_chat(alice)

    response = alice.patch(f"/api/session/{chat}/settings", json={"workspace": str(tmp_path)})

    assert response.status_code == 403
    assert not alice.get(f"/api/session/{chat}/settings").json()["settings"].get("workspace")


def test_a_path_that_is_not_a_folder_is_refused(api, tmp_path):
    admin = api.as_admin()
    chat = _new_chat(admin)
    not_a_folder = tmp_path / "notes.txt"
    not_a_folder.write_text("x", encoding="utf-8")

    response = admin.patch(f"/api/session/{chat}/settings", json={"workspace": str(not_a_folder)})

    assert response.status_code == 400


def test_clearing_needs_no_checks(api):
    alice = api.as_user("alice")
    chat = _new_chat(alice)

    assert alice.patch(f"/api/session/{chat}/settings", json={"workspace": None}).status_code == 200
