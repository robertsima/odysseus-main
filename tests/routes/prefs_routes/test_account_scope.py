"""/api/prefs keeps one preference store per signed-in account.

The theme, navigation order and page style sync through these routes so they
follow the account to another browser. Keyed by anything but the signed-in
user, one user's theme would show up for everyone on the deployment.
"""
import pytest

import routes.prefs_routes as prefs_routes

pytestmark = pytest.mark.security


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    path = tmp_path / "user_prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(path))
    return path


def test_a_preference_one_user_writes_is_not_read_by_another(api, prefs_file):
    alice, bob = api.as_user("alice"), api.as_user("bob")

    written = alice.put("/api/prefs/theme", json={"value": {"name": "ume", "updated_at": 5}})
    assert written.status_code == 200, written.text

    assert alice.get("/api/prefs/theme").json()["value"] == {"name": "ume", "updated_at": 5}
    assert bob.get("/api/prefs/theme").json()["value"] is None
    assert "theme" not in bob.get("/api/prefs").json()


def test_each_users_write_leaves_the_others_value_alone(api, prefs_file):
    alice, bob = api.as_user("alice"), api.as_user("bob")

    alice.put("/api/prefs/nav-order", json={"value": ["notes", "tasks"]})
    bob.put("/api/prefs/nav-order", json={"value": ["tasks", "notes"]})

    assert alice.get("/api/prefs/nav-order").json()["value"] == ["notes", "tasks"]
    assert bob.get("/api/prefs/nav-order").json()["value"] == ["tasks", "notes"]
