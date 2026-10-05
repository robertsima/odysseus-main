"""Route a worker's actual managed path without requiring user setup."""
import json

import pytest

from src.agent_tools.worktree_tools import AgentWorktreeTool
from src.agent_worktree import service, config
from tests.src.agent_worktree.gitcmd.test_agent_worktree_service import (
    _prepare_change, cfg, repo, git_required,
)

pytestmark = [pytest.mark.security, git_required]


async def test_default_layout_worktree_path_routes_to_named_status_and_diagnose(cfg, monkeypatch):
    path = await _prepare_change(cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    status = await AgentWorktreeTool().execute(json.dumps({"action": "status", "repository": path}), {})
    assert status["exit_code"] == 0
    assert status["status"]["repository"] == cfg.source_repo
    assert status["status"]["branch"] == "agent/odysseus/task"
    diagnosis = await AgentWorktreeTool().execute(json.dumps({
        "action": "diagnose", "repository": path,
        "expected_head": status["status"]["head_sha"],
    }), {})
    assert diagnosis["exit_code"] == 0
    assert diagnosis["diagnosis"]["code"] == "READY"
    assert diagnosis["diagnosis"]["network_used"] is False


async def test_tool_head_mismatch_returns_complete_diagnostic_call(cfg, monkeypatch):
    await _prepare_change(cfg)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    expected = "a" * 40
    result = await AgentWorktreeTool().execute(json.dumps({
        "action": "request_publish", "repository": cfg.source_repo,
        "branch": "agent/odysseus/task", "expected_head": expected, "title": "Tested",
    }), {})
    assert result["code"] == "HEAD_MISMATCH"
    assert result["next_action"] == {
        "action": "diagnose", "repository": cfg.source_repo,
        "branch": "agent/odysseus/task", "expected_head": expected,
    }


async def test_invalid_expected_head_refuses_before_starting_or_network(cfg):
    with pytest.raises(service.WorktreeError) as caught:
        await service.request_publish("missing", title="x", cfg=cfg, expected_head="--all")
    assert caught.value.code == "INVALID_EXPECTED_HEAD"
