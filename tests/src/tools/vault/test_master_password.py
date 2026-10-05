"""The vault_unlock tool hands the master password to bw on stdin, not argv.

Process arguments are readable by every local user through ``ps`` and
/proc/<pid>/cmdline, and the master password decrypts the whole vault.
"""
import asyncio
import json

import pytest

from src.tools import vault

pytestmark = pytest.mark.security

SECRET = "correct horse battery staple"


async def test_the_unlock_tool_passes_the_master_password_on_stdin(tmp_path, monkeypatch):
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self, input=None):
            calls[-1]["stdin"] = input
            return b"session-key", b""

    async def create_subprocess_exec(*argv, env=None, **kwargs):
        calls.append({"argv": [str(a) for a in argv], "env": dict(env or {})})
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(vault, "VAULT_FILE", str(tmp_path / "vault.json"))

    result = await vault.do_vault_unlock(json.dumps({"master_password": SECRET}))

    assert result["exit_code"] == 0, result
    [call] = calls
    assert all(SECRET not in arg for arg in call["argv"])
    assert SECRET not in json.dumps(call["env"])
    assert call["stdin"] == (SECRET + "\n").encode()
