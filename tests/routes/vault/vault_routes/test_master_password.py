"""The Bitwarden master password never reaches the bw command line.

Process arguments are readable by every local user through ``ps`` and
/proc/<pid>/cmdline, and the master password decrypts the whole vault, so the
vault routes hand it to ``bw`` on stdin.
"""
import asyncio
import json

import pytest

from routes.vault import vault_routes

pytestmark = pytest.mark.security

SECRET = "correct horse battery staple"


class _Proc:
    returncode = 0

    def __init__(self, calls):
        self.calls = calls

    async def communicate(self, input=None):
        self.calls[-1]["stdin"] = input
        return b"session-key", b""


@pytest.fixture
def bw_calls(monkeypatch):
    calls = []

    async def create_subprocess_exec(*argv, env=None, **kwargs):
        calls.append({"argv": [str(a) for a in argv], "env": dict(env or {})})
        return _Proc(calls)

    monkeypatch.setattr(vault_routes, "_find_bw", lambda: "bw")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    return calls


@pytest.mark.parametrize("path,body", [
    ("/api/vault/unlock", {"master_password": SECRET}),
    ("/api/vault/login", {"email": "admin@example.test", "master_password": SECRET}),
], ids=["unlock", "login"])
def test_the_master_password_goes_to_bw_on_stdin(api, bw_calls, path, body):
    response = api.as_admin().post(path, json=body)

    assert response.status_code == 200, response.text
    [call] = bw_calls
    assert all(SECRET not in arg for arg in call["argv"])
    assert SECRET not in json.dumps(call["env"])
    assert call["stdin"] == (SECRET + "\n").encode()


def test_only_an_admin_may_unlock_the_vault(api, bw_calls):
    response = api.as_user("alice").post("/api/vault/unlock", json={"master_password": SECRET})

    assert response.status_code == 403
    assert bw_calls == []


async def test_a_password_passed_by_environment_stays_out_of_argv(bw_calls):
    await vault_routes._run_bw(["unlock", "--passwordenv", "BW_PASSWORD", "--raw"], bw_password=SECRET)

    [call] = bw_calls
    assert call["env"]["BW_PASSWORD"] == SECRET
    assert all(SECRET not in arg for arg in call["argv"])


async def test_no_password_means_no_password_variable(bw_calls):
    await vault_routes._run_bw(["lock"])

    assert "BW_PASSWORD" not in bw_calls[0]["env"]


def test_a_config_file_that_is_not_an_object_reads_as_empty(tmp_path, monkeypatch):
    vault_file = tmp_path / "vault.json"
    vault_file.write_text(json.dumps(["not", "a", "config", "object"]), encoding="utf-8")
    monkeypatch.setattr(vault_routes, "VAULT_FILE", vault_file)

    assert vault_routes._load_config() == {}
