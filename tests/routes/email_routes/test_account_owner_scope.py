"""A user sees and changes only their own mail accounts, and cannot aim a mailbox call at someone else's."""
import pytest

pytestmark = pytest.mark.security

ACCOUNT = {
    "name": "Alice work",
    "imap_host": "imap.alice.example",
    "imap_user": "alice@alice.example",
    "imap_password": "alice-imap-secret",
    "smtp_host": "smtp.alice.example",
    "smtp_user": "alice@alice.example",
    "smtp_password": "alice-smtp-secret",
    "from_address": "alice@alice.example",
}


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


@pytest.fixture
def alice_account(alice):
    response = alice.post("/api/email/accounts", json=ACCOUNT)
    assert response.json()["ok"] is True
    return response.json()["id"]


def test_the_owner_lists_updates_and_deletes_their_account(alice, alice_account):
    listed = alice.get("/api/email/accounts").json()["accounts"]
    assert [a["id"] for a in listed] == [alice_account]
    assert listed[0]["has_imap_password"] is True
    assert "alice-imap-secret" not in alice.get("/api/email/accounts").text

    assert alice.put(f"/api/email/accounts/{alice_account}", json={"name": "Renamed"}).json()["ok"] is True
    assert alice.get("/api/email/accounts").json()["accounts"][0]["name"] == "Renamed"

    assert alice.delete(f"/api/email/accounts/{alice_account}").json() == {"ok": True}
    assert alice.get("/api/email/accounts").json()["accounts"] == []


def test_the_account_list_shows_only_the_callers_accounts(bob, alice_account):
    response = bob.get("/api/email/accounts")

    assert response.status_code == 200
    assert response.json()["accounts"] == []
    assert "alice.example" not in response.text


@pytest.mark.parametrize("method, path, body", [
    ("PUT", "/api/email/accounts/{id}", {"name": "hijacked", "imap_host": "evil.example"}),
    ("DELETE", "/api/email/accounts/{id}", None),
    ("POST", "/api/email/accounts/{id}/set-default", None),
])
def test_another_user_cannot_change_or_delete_an_account(alice, bob, alice_account, method, path, body):
    response = bob.request(method, path.format(id=alice_account), json=body)

    assert response.status_code in (403, 404)
    mine = alice.get("/api/email/accounts").json()["accounts"]
    assert [(a["name"], a["imap_host"]) for a in mine] == [("Alice work", "imap.alice.example")]


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/email/list?account_id={id}"),
    ("GET", "/api/email/folders?account_id={id}"),
    ("GET", "/api/email/search?q=x&account_id={id}"),
    ("GET", "/api/email/read/1?account_id={id}"),
    ("GET", "/api/email/attachments/1?account_id={id}"),
    ("POST", "/api/email/mark-read/1?account_id={id}"),
    ("DELETE", "/api/email/delete/1?account_id={id}"),
    ("GET", "/api/email/style?account_id={id}"),
    ("PUT", "/api/email/style?account_id={id}"),
])
def test_another_user_cannot_aim_a_mailbox_call_at_an_account(bob, alice_account, method, path):
    response = bob.request(method, path.format(id=alice_account), json={"style": "x"})

    assert response.status_code in (403, 404)
