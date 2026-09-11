"""Built-in GitHub MCP server registration.

The server is the official github-mcp-server binary run as a stdio child
process of the Odysseus container. These tests pin the two things that are
easy to break silently: the token actually reaching the subprocess, and the
tool surface staying narrow (read-only by default, no repo-mutating tools).
"""

import pytest

from src import builtin_mcp


DESTRUCTIVE_TOOLS = {
    "push_files",
    "create_or_update_file",
    "delete_repository",
    "delete_file",
    "merge_pull_request",
}


@pytest.fixture(autouse=True)
def _binary_on_disk(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_GITHUB_MCP_BINARY", "/usr/local/bin/github-mcp-server")
    monkeypatch.delenv("ODYSSEUS_GITHUB_MCP_WRITE", raising=False)
    monkeypatch.delenv("GITHUB_HOST", raising=False)


def test_no_servers_without_a_token(monkeypatch):
    monkeypatch.delenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, raising=False)
    assert builtin_mcp.github_mcp_servers() == {}


def test_a_token_that_is_not_a_github_token_is_called_out(monkeypatch, caplog):
    """An Odysseus `ody_` token pasted into GITHUB_PERSONAL_ACCESS_TOKEN starts
    the server and then fails every call; say so at startup instead."""
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ody_QDpRv0I7Azf-not-a-github-token")
    with caplog.at_level("WARNING", logger="src.builtin_mcp"):
        servers = builtin_mcp.github_mcp_servers()
    assert set(servers) == {"github_read"}  # still registered; the operator decides
    assert any("does not look like a GitHub token" in r.message and "'ody_'" in r.message for r in caplog.records)
    assert not any("QDpRv0I7" in r.message for r in caplog.records)


@pytest.mark.parametrize("token", ["ghp_abc123", "github_pat_11ABC_def", "gho_x", "a" * 40])
def test_real_github_token_shapes_do_not_warn(monkeypatch, caplog, token):
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, token)
    with caplog.at_level("WARNING", logger="src.builtin_mcp"):
        builtin_mcp.github_mcp_servers()
    assert not any("does not look like a GitHub token" in r.message for r in caplog.records)


def test_no_servers_when_binary_is_missing(monkeypatch):
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ghp_token")
    monkeypatch.delenv("ODYSSEUS_GITHUB_MCP_BINARY", raising=False)
    monkeypatch.setattr(builtin_mcp, "which_tool", lambda name: None)
    monkeypatch.setattr(builtin_mcp.os.path, "isfile", lambda path: False)
    assert builtin_mcp.github_mcp_servers() == {}


def test_read_server_is_read_only_and_tool_scoped(monkeypatch):
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ghp_token")

    servers = builtin_mcp.github_mcp_servers()

    assert set(servers) == {"github_read"}
    args = servers["github_read"]["args"]
    assert args[0] == "stdio"
    assert "--read-only" in args
    assert not any(a.startswith("--toolsets") for a in args)

    tools = _tools_from_args(args)
    assert tools == set(builtin_mcp.GITHUB_MCP_READ_TOOLS)
    assert not tools & DESTRUCTIVE_TOOLS


def test_write_server_is_opt_in_and_never_repo_mutating(monkeypatch):
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ghp_token")
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_WRITE_ENV, "1")

    servers = builtin_mcp.github_mcp_servers()

    assert set(servers) == {"github_read", "github_write"}
    args = servers["github_write"]["args"]
    # --read-only would drop every tool the write server exists to offer.
    assert "--read-only" not in args
    assert not any(a.startswith("--toolsets") for a in args)

    tools = _tools_from_args(args)
    assert tools == set(builtin_mcp.GITHUB_MCP_WRITE_TOOLS)
    assert not tools & DESTRUCTIVE_TOOLS


def test_token_is_passed_explicitly_to_the_subprocess(monkeypatch):
    # The MCP SDK does not inherit the parent environment when env is None --
    # get_default_environment() forwards only PATH/HOME/SHELL/TERM/USER -- so
    # an empty env dict here means an unauthenticated server.
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ghp_token")

    env = builtin_mcp.github_mcp_env()

    assert env[builtin_mcp.GITHUB_MCP_TOKEN_ENV] == "ghp_token"
    assert "GITHUB_HOST" not in env


def test_enterprise_host_is_forwarded_when_set(monkeypatch):
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ghp_token")
    monkeypatch.setenv("GITHUB_HOST", "https://github.example.com")

    assert builtin_mcp.github_mcp_env()["GITHUB_HOST"] == "https://github.example.com"


def test_env_keys_avoid_the_identity_hint_scan(monkeypatch):
    # McpManager echoes env values whose key contains user/account/email into
    # tool descriptions to tell instances apart; a token must not land there.
    monkeypatch.setenv(builtin_mcp.GITHUB_MCP_TOKEN_ENV, "ghp_token")
    monkeypatch.setenv("GITHUB_HOST", "https://github.example.com")

    for key in builtin_mcp.github_mcp_env():
        lowered = key.lower()
        assert not any(hint in lowered for hint in ("email_address", "account", "user", "username"))


def _tools_from_args(args):
    flag = next(a for a in args if a.startswith("--tools="))
    return set(flag.split("=", 1)[1].split(","))


def test_github_servers_are_builtin_and_function_callable():
    # Built-in servers are dropped from the function-calling schemas unless
    # they opt in; the Python built-ins are described in the agent prompt
    # instead, but these are external binaries with no prompt entry, so
    # missing either set leaves their tools invisible to the model.
    from src.mcp_manager import McpManager, _BUILTIN_FUNCTION_CALLING_SERVERS

    manager = McpManager()
    for server_id in ("github_read", "github_write"):
        assert manager.is_builtin(server_id)
        assert server_id in _BUILTIN_FUNCTION_CALLING_SERVERS
