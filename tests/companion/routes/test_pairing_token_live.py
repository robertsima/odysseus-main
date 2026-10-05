"""Minting a companion pairing code through the real app."""
import pytest


def _pair(client):
    response = client.post("/api/companion/pair?format=json")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_fresh_pairing_code_authenticates_on_the_next_request(api):
    # Minting must refresh the auth middleware's token cache; a stale cache
    # makes the QR code fail until the server restarts.
    client = api.anonymous()
    stale = client.get("/api/codex/plugin.zip", headers={"Authorization": "Bearer ody_warmup000000"})
    assert stale.status_code == 401
    paired = _pair(api.as_admin())

    response = client.get(
        "/api/codex/plugin.zip", headers={"Authorization": f"Bearer {paired['token']}"}
    )

    assert response.status_code == 200
    assert paired["payload"]["token"] == paired["token"]


@pytest.mark.security
def test_only_an_admin_can_mint_a_pairing_code(api):
    assert api.as_user("alice").post("/api/companion/pair?format=json").status_code == 403
    assert api.anonymous().post("/api/companion/pair?format=json").status_code == 401
