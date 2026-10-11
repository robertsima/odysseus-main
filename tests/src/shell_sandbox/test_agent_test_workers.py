"""`pytest -n auto` typed into an agent shell must not start one worker per CPU.

`tests.run` bounds the workers it starts itself. A worker that ran `pytest -n auto` directly still
got one per CPU, each importing the whole app inside the app's container (2026-10-10).
"""
import importlib.util
import os
import subprocess
import sys

import pytest

from src import shell_sandbox

VAR = "PYTEST_XDIST_AUTO_NUM_WORKERS"


@pytest.mark.parametrize("build", [shell_sandbox.sandbox_env, shell_sandbox.agent_shell_env],
                         ids=["sandboxed", "unsandboxed"])
def test_agent_shell_env_bounds_xdist_auto(build):
    assert build({})[VAR] == "2"


@pytest.mark.parametrize("build", [shell_sandbox.sandbox_env, shell_sandbox.agent_shell_env],
                         ids=["sandboxed", "unsandboxed"])
def test_an_operator_set_xdist_worker_count_is_left_alone(build):
    assert build({VAR: "5"})[VAR] == "5"


@pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="pytest-xdist is not installed")
def test_pytest_n_auto_starts_two_workers_under_the_agent_shell_env(tmp_path):
    """The env the shell builds, not a mocked one, decides what `-n auto` means."""
    (tmp_path / "test_two.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    env = {**os.environ, **shell_sandbox.agent_shell_env({})}
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(key, None)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "xdist", "-n", "auto", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), str(tmp_path / "test_two.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert "created: 2/2 workers" in done.stdout, done.stdout[-800:] + done.stderr[-800:]
