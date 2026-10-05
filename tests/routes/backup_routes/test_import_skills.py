"""Importing a backup's skills through the real app and the real skills store."""


def _names(client):
    return {s["name"] for s in client.get("/api/skills").json()["skills"]}


def test_an_empty_skills_list_in_a_backup_imports_cleanly(api):
    # Exported backups always carry a "skills" key, often empty. The importer once
    # called a removed save() on it and answered 500 with an HTML page.
    admin = api.as_admin()
    before = _names(admin)

    response = admin.post("/api/import", json={"settings": {}, "skills": []})

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert _names(admin) == before


def test_a_skill_in_a_backup_is_restored_for_the_importing_user(api):
    admin = api.as_admin()
    assert "buy-milk" not in _names(admin)
    body = {"skills": [{"name": "buy-milk", "title": "Buy milk", "description": "Buy milk"}]}

    response = admin.post("/api/import", json=body)

    assert response.status_code == 200, response.text
    assert "1 skills" in response.json()["imported"]
    assert "buy-milk" in _names(admin)
    assert "buy-milk" not in _names(api.as_user("alice"))
