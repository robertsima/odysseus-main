"""The shell endpoints run commands on the server, so only admins may call them.

The command is blank, so a request that wrongly passes the gate still runs
nothing: the handler answers "No command provided" without starting a shell.
"""
import pytest

pytestmark = pytest.mark.security

SHELL_ENDPOINTS = ("/api/shell/exec", "/api/shell/stream")
BLANK_COMMAND = {"command": " "}


@pytest.mark.parametrize("path", SHELL_ENDPOINTS)
def test_a_logged_in_user_who_is_not_an_admin_is_refused(api, path):
    response = api.as_user("alice").post(path, json=BLANK_COMMAND)

    assert response.status_code == 403


@pytest.mark.parametrize("path", SHELL_ENDPOINTS)
def test_an_admin_is_let_through(api, path):
    response = api.as_admin().post(path, json=BLANK_COMMAND)

    assert response.status_code == 200
    assert "No command provided" in response.text
