import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

import mcp_servers.todoist_server as srv


class FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.kill = Mock()
        self.wait = AsyncMock()

    async def communicate(self):
        return self._stdout, self._stderr


def _text(result):
    return result[0].text


def test_missing_todoist_api_token_returns_useful_error(monkeypatch):
    monkeypatch.delenv(srv.TOKEN_ENV_VAR, raising=False)

    result = asyncio.run(srv.call_tool("todoist", {"args": ["today", "--json"]}))

    assert srv.TOKEN_ENV_VAR in _text(result)
    assert "not configured" in _text(result)


def test_missing_td_executable_returns_useful_error(monkeypatch):
    monkeypatch.setenv(srv.TOKEN_ENV_VAR, "secret-token")

    with patch("mcp_servers.todoist_server.shutil.which", return_value=None):
        result = asyncio.run(srv.call_tool("todoist", {"args": ["today"]}))

    assert "td" in _text(result)
    assert "not found" in _text(result)
    assert "secret-token" not in _text(result)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"args": "today"},
        {"args": ["today", 3]},
        None,
    ],
)
def test_invalid_args_are_rejected(arguments):
    result = asyncio.run(srv.call_tool("todoist", arguments))

    assert "array of strings" in _text(result)


def test_successful_subprocess_stdout_is_returned(monkeypatch):
    monkeypatch.setenv(srv.TOKEN_ENV_VAR, "secret-token")
    proc = FakeProcess(stdout=b'{"tasks":[]}\n')

    with patch("mcp_servers.todoist_server.shutil.which", return_value="/usr/local/bin/td"), \
         patch("mcp_servers.todoist_server.asyncio.create_subprocess_exec", return_value=proc):
        result = asyncio.run(srv.call_tool("todoist", {"args": ["today", "--json"]}))

    assert _text(result) == '{"tasks":[]}'


def test_nonzero_exit_status_returns_cli_error(monkeypatch):
    monkeypatch.setenv(srv.TOKEN_ENV_VAR, "secret-token")
    proc = FakeProcess(returncode=2, stderr=b"Bad command\n")

    with patch("mcp_servers.todoist_server.shutil.which", return_value="/usr/local/bin/td"), \
         patch("mcp_servers.todoist_server.asyncio.create_subprocess_exec", return_value=proc):
        result = asyncio.run(srv.call_tool("todoist", {"args": ["bad"]}))

    assert "td exited with status 2" in _text(result)
    assert "Bad command" in _text(result)


def test_timeout_is_handled(monkeypatch):
    monkeypatch.setenv(srv.TOKEN_ENV_VAR, "secret-token")
    proc = FakeProcess()

    async def never_finishes():
        await asyncio.sleep(10)

    proc.communicate = never_finishes

    with patch("mcp_servers.todoist_server.shutil.which", return_value="/usr/local/bin/td"), \
         patch("mcp_servers.todoist_server.asyncio.create_subprocess_exec", return_value=proc):
        returncode, stdout, stderr = asyncio.run(srv._run_td(["today"], timeout=0.01))

    assert returncode == 1
    assert stdout == ""
    assert "timed out" in stderr
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()


def test_arguments_are_passed_directly_without_shell(monkeypatch):
    monkeypatch.setenv(srv.TOKEN_ENV_VAR, "secret-token")
    proc = FakeProcess(stdout=b"ok")

    with patch("mcp_servers.todoist_server.shutil.which", return_value="/usr/local/bin/td"), \
         patch("mcp_servers.todoist_server.asyncio.create_subprocess_exec", return_value=proc) as create_proc:
        result = asyncio.run(srv.call_tool("todoist", {"args": ["task", "list", "--json"]}))

    assert _text(result) == "ok"
    create_proc.assert_called_once()
    args, kwargs = create_proc.call_args
    assert args == ("/usr/local/bin/td", "task", "list", "--json")
    assert "shell" not in kwargs
