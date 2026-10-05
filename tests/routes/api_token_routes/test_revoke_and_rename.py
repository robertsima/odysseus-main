"""Deleting and renaming API tokens through the real app."""
import pytest

from core.database import ApiToken, get_db_session


def _create(client, **fields):
    response = client.post("/api/tokens", data=fields)
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_foreign_token(token_id="foreign1", owner="carol"):
    with get_db_session() as db:
        db.add(ApiToken(
            id=token_id, owner=owner, name="carols", token_hash="x", token_prefix="ody_carl",
            scopes="chat", is_active=True,
        ))
    return token_id


def _listed_ids(api):
    return {row["id"] for row in api.as_admin().get("/api/tokens").json()}


def test_a_deleted_token_stops_authenticating_at_once(api):
    admin = api.as_admin()
    token = _create(admin, name="ci", scopes="chat")
    probe = "/api/codex/plugin.zip"
    assert api.anonymous().get(probe, headers=_bearer(token["token"])).status_code == 200

    response = admin.delete(f"/api/tokens/{token['id']}")

    assert response.json() == {"status": "deleted"}
    assert token["id"] not in _listed_ids(api)
    # The auth middleware caches tokens; a missed invalidation keeps the revoked one alive.
    assert api.anonymous().get(probe, headers=_bearer(token["token"])).status_code == 401


def test_deleting_a_missing_token_is_a_404(api):
    assert api.as_admin().delete("/api/tokens/nope1234").status_code == 404


@pytest.mark.security
def test_a_token_owned_by_someone_else_cannot_be_deleted_or_renamed(api):
    token_id = _seed_foreign_token()
    admin = api.as_admin()

    assert admin.delete(f"/api/tokens/{token_id}").status_code == 403
    assert admin.patch(f"/api/tokens/{token_id}", json={"name": "mine now"}).status_code == 403

    row = next(r for r in admin.get("/api/tokens").json() if r["id"] == token_id)
    assert row["name"] == "carols"


@pytest.mark.security
def test_a_regular_user_cannot_delete_a_token(api):
    token = _create(api.as_admin(), name="ci")

    assert api.as_user("alice").delete(f"/api/tokens/{token['id']}").status_code == 403
    assert token["id"] in _listed_ids(api)


def test_renaming_a_token_keeps_its_scopes(api):
    admin = api.as_admin()
    token = _create(admin, name="original", scopes="todos:write")

    response = admin.patch(f"/api/tokens/{token['id']}", json={"name": "updated"})

    assert response.status_code == 200
    assert response.json()["name"] == "updated"
    stored = next(r for r in admin.get("/api/tokens").json() if r["id"] == token["id"])
    assert stored["name"] == "updated"
    assert stored["scopes"] == ["todos:read", "todos:write"]
