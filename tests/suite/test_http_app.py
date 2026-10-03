"""tests/plugins/http_app.py drives the real app as a known user and keeps off the real data folder."""
import os

import pytest

from tests.plugins import http_app


@pytest.mark.parametrize("who, username, is_admin", [
    ("alice", "alice", False),
    ("bob", "bob", False),
    ("admin", "admin", True),
])
def test_each_client_is_signed_in_as_its_user(api, who, username, is_admin):
    client = api.as_admin() if who == "admin" else api.as_user(who)

    status = client.get("/api/auth/status").json()

    assert (status["authenticated"], status["username"], status["is_admin"]) == (True, username, is_admin)


def test_the_anonymous_client_is_turned_away(api):
    assert api.anonymous().get("/api/sessions").status_code == 401


def test_opening_a_file_in_the_real_data_folder_is_stopped(api):
    target = os.path.join(http_app._PROTECTED[0], "written-by-a-test.txt")

    with pytest.raises(PermissionError, match="real data folder"):
        open(target, "w", encoding="utf-8")

    assert not os.path.exists(target)
    http_app._guard["hits"].clear()  # the guard did its job; don't fail this test for it
