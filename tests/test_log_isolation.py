"""A test run never writes into the deployment's app.log (2026-09-22 audit)."""

import logging
import logging.handlers
import os


def test_importing_the_app_under_pytest_installs_no_file_handler():
    import app  # noqa: F401  (module import installs the logging handlers)

    handlers = logging.getLogger().handlers
    assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)
    assert os.environ.get("ODYSSEUS_FILE_LOG") == "0"


def test_trace_gathers_one_id_across_rotated_logs(tmp_path, monkeypatch):
    from src import agent_logs

    (tmp_path / "app.log.1").write_text("2026-09-21 22:00:00 - x - INFO - start workflow=workflow-abc123 token sk-aaaaaaaaaaaaaaaaaaaaaa\n")
    (tmp_path / "app.log").write_text("2026-09-21 22:05:00 - x - INFO - terminal workflow=workflow-abc123\n"
                                      "2026-09-21 22:05:01 - x - INFO - unrelated\n")
    os.utime(tmp_path / "app.log.1", (1, 1))
    monkeypatch.setattr(agent_logs, "log_roots", lambda: [str(tmp_path)])
    monkeypatch.setattr("src.agent_activity.get_run", lambda rid: {
        "run_id": rid, "status": "partial", "owner": "u", "summary": {"workflow_id": rid}})
    monkeypatch.setattr("src.agent_activity.list_runs", lambda **k: [])
    found = agent_logs.trace("workflow-abc123", owner="u")
    assert found["line_count"] == 2
    assert found["lines"][0].startswith("app.log.1:") and "sk-aaaa" not in found["lines"][0]
    assert found["runs"][0]["status"] == "partial"


def test_trace_refuses_a_non_id():
    import pytest
    from src import agent_logs

    with pytest.raises(RuntimeError):
        agent_logs.trace("../../etc/passwd")
