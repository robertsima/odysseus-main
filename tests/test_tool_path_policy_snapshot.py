"""A directory walk computes the path policy's config inputs once.

Every directory glob/grep/ls visits is checked against the path policy, and
each check re-read and re-parsed the agent-worktree config (twice) plus several
settings. Two globs over /app took 97 s each on the server. Inside
run_with_policy_snapshot those inputs are computed once per walk; outside it
nothing is cached, so single-path checks always see current config.
"""
import os
from types import SimpleNamespace

from src import tool_execution as te


def _count_config_loads(monkeypatch, tmp_path):
    loads = {"n": 0}
    state = tmp_path / "worktree-state"
    state.mkdir()

    def load_config():
        loads["n"] += 1
        return SimpleNamespace(state_dir=str(state), private_key_path=str(tmp_path / "app-key.pem"))

    import src.agent_worktree.config as cfg
    monkeypatch.setattr(cfg, "load_config", load_config)
    return loads, state


def test_config_is_loaded_once_per_walk(monkeypatch, tmp_path):
    loads, _ = _count_config_loads(monkeypatch, tmp_path)
    dirs = []
    for i in range(20):
        d = tmp_path / f"d{i}"
        d.mkdir()
        dirs.append(os.path.realpath(d))

    def walk():
        return [te._can_traverse_tool_path(d) for d in dirs]

    assert all(te.run_with_policy_snapshot(walk))
    assert loads["n"] == 1

    loads["n"] = 0
    walk()  # no snapshot: every check reads current config
    assert loads["n"] >= len(dirs)


def test_policy_still_refuses_inside_a_snapshot(monkeypatch, tmp_path):
    _, state = _count_config_loads(monkeypatch, tmp_path)
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = tmp_path / "app-key.pem"
    key.write_text("k")

    def checks():
        return {
            "state_dir": te._is_sensitive_path(os.path.realpath(state)),
            "inside_state": te._is_sensitive_path(os.path.join(os.path.realpath(state), "grant.json")),
            "signing_key": te._is_sensitive_path(os.path.realpath(key)),
            "ssh": te._can_traverse_tool_path(os.path.realpath(ssh)),
            "plain": te._is_sensitive_path(os.path.realpath(tmp_path / "notes.txt")),
        }

    outside = checks()
    inside = te.run_with_policy_snapshot(checks)
    assert inside == outside
    assert inside["state_dir"] and inside["inside_state"] and inside["signing_key"]
    assert inside["ssh"] is False and inside["plain"] is False


def test_snapshot_does_not_outlive_the_walk(monkeypatch, tmp_path):
    loads, _ = _count_config_loads(monkeypatch, tmp_path)
    te.run_with_policy_snapshot(lambda: te._is_sensitive_path(os.path.realpath(tmp_path)))
    assert te._POLICY_SNAPSHOT.get() is None
    loads["n"] = 0
    te._is_sensitive_path(os.path.realpath(tmp_path))
    assert loads["n"] >= 1
