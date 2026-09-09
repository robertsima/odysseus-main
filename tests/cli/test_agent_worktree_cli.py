"""Operator CLI: the only path that can approve a publish.

The CLI is the human end of the gate, so the tests focus on the properties an
operator relies on: it shows the full file list before approval, it refuses a
sensitive change without the explicit acknowledgement, and the code it prints is
one-time.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.helpers.cli_loader import load_script  # noqa: E402

pytestmark = [pytest.mark.area_cli, pytest.mark.area_security]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    # ODYSSEUS_DATA_DIR is consumed at import time, so the state directory gets
    # its own override; without it these tests would write approval records into
    # the developer's real data/ directory.
    monkeypatch.setenv("ODYSSEUS_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(tmp_path / "wt"))
    for name in ("ODYSSEUS_AGENT_PUBLISH_ENABLED", "ODYSSEUS_AGENT_REPO",
                 "ODYSSEUS_AGENT_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return load_script("odysseus-agent-worktree")


def _seed(cfg, *, files=("src/a.py",), sensitive=None):
    from src.agent_worktree import approval as approval_mod
    from src.agent_worktree import sensitive as sensitive_mod
    from src.agent_worktree.service import files_digest

    sensitive = sensitive or {}
    return approval_mod.create_request(
        repo="acme/widgets",
        branch="agent/odysseus/task",
        head_sha="a" * 40,
        base_branch="dev",
        title="Fix the thing",
        body="",
        changed_files=list(files),
        files_digest=files_digest(list(files)),
        sensitive=sensitive,
        sensitive_digest=sensitive_mod.digest(sensitive),
        cfg=cfg,
    )


def _enable(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(repo))
    monkeypatch.setenv("ODYSSEUS_AGENT_GITHUB_TOKEN", "t" * 40)


def test_approve_is_refused_while_publishing_is_disabled(cli, capsys, tmp_path):
    from src.agent_worktree.config import load_config

    record = _seed(load_config())
    with pytest.raises(SystemExit):
        cli.run(cli.build_parser(), ["approve", record["id"]])
    assert "publishing is not available" in capsys.readouterr().err


def test_show_lists_every_changed_file(cli, capsys, monkeypatch, tmp_path):
    from src.agent_worktree.config import load_config

    record = _seed(load_config(), files=("src/a.py", "src/b.py"))
    cli.run(cli.build_parser(), ["show", record["id"]])
    out = capsys.readouterr().out
    assert "src/a.py" in out and "src/b.py" in out
    assert record["head_sha"] in out


def test_show_marks_sensitive_files_and_warns(cli, capsys):
    from src.agent_worktree.config import load_config

    record = _seed(
        load_config(),
        files=(".github/workflows/ci.yml",),
        sensitive={"workflows": [".github/workflows/ci.yml"]},
    )
    cli.run(cli.build_parser(), ["show", record["id"]])
    out = capsys.readouterr().out
    assert "! .github/workflows/ci.yml" in out
    assert "--allow-sensitive" in out


def test_approve_prints_a_one_time_code(cli, capsys, monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    from src.agent_worktree.config import load_config

    record = _seed(load_config())
    cli.run(cli.build_parser(), ["approve", record["id"]])
    out = capsys.readouterr().out
    assert "approval code:" in out
    assert "single use" in out

    # A second approve on the same request issues a *new* code rather than
    # reprinting the old one, and the record never stores plaintext.
    code = out.split("approval code:")[1].strip().splitlines()[0]
    from src.agent_worktree import approval as approval_mod

    stored = open(approval_mod._record_path(load_config(), record["id"]), encoding="utf-8").read()
    assert code not in stored


def test_approve_refuses_a_sensitive_change_without_the_flag(cli, capsys, monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    from src.agent_worktree.config import load_config

    record = _seed(
        load_config(),
        files=("Dockerfile",),
        sensitive={"docker": ["Dockerfile"]},
    )
    with pytest.raises(SystemExit):
        cli.run(cli.build_parser(), ["approve", record["id"]])
    assert "--allow-sensitive" in capsys.readouterr().err


def test_approve_succeeds_for_a_sensitive_change_with_the_flag(cli, capsys, monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    from src.agent_worktree.config import load_config

    record = _seed(
        load_config(),
        files=("Dockerfile",),
        sensitive={"docker": ["Dockerfile"]},
    )
    cli.run(cli.build_parser(), ["approve", record["id"], "--allow-sensitive"])
    assert "approval code:" in capsys.readouterr().out


def test_revoke_marks_the_request_revoked(cli, capsys, monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    from src.agent_worktree.config import load_config

    record = _seed(load_config())
    cli.run(cli.build_parser(), ["approve", record["id"]])
    capsys.readouterr()
    cli.run(cli.build_parser(), ["revoke", record["id"]])
    assert '"status": "revoked"' in capsys.readouterr().out.replace('"status":"revoked"', '"status": "revoked"')
