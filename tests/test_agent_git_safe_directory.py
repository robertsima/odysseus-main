"""The agent's git must be able to open checkouts on the data volume.

2026-09-11: the container runs as root, /app/data belongs to uid 1000, and
every git command in /app/data/development/dog-trainer failed with "dubious
ownership" — or, for ls-remote, the misleading "'origin' does not appear to
be a git repository". The exception used to live in /root/.gitconfig and a
rebuild removed it. The shell tool now carries it in its environment.
"""

import pytest

from src import tool_execution

pytestmark = pytest.mark.area_security


def test_safe_directory_covers_the_data_dir_and_everything_under_it():
    env = tool_execution._git_safe_directory_env({})
    assert env["GIT_CONFIG_COUNT"] == "2"
    root = tool_execution._AGENT_WORKDIR.rstrip("/\\")
    pairs = {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(2)}
    assert pairs == {("safe.directory", root), ("safe.directory", f"{root}/*")}


def test_an_operators_own_git_config_entries_are_kept():
    env = tool_execution._git_safe_directory_env(
        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "ody"}
    )
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert "GIT_CONFIG_KEY_0" not in env  # untouched, still the operator's
    assert env["GIT_CONFIG_KEY_1"] == env["GIT_CONFIG_KEY_2"] == "safe.directory"


def test_a_garbage_count_does_not_take_the_shell_tool_down():
    env = tool_execution._git_safe_directory_env({"GIT_CONFIG_COUNT": "lots"})
    assert env["GIT_CONFIG_COUNT"] == "2"


def test_the_shell_tool_environment_carries_it(monkeypatch):
    """_direct_fallback builds the env inline; pin that it goes through the
    helper by checking the source, since running the fallback needs a tool."""
    import inspect

    source = inspect.getsource(tool_execution._direct_fallback)
    assert "_git_safe_directory_env(_subproc_env)" in source
