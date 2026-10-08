"""manage_agent_worktree sync / publish_sync as an agent calls them.

2026-10-07/08: the tool had no way to bring the base into a worktree, so a
PR that fell behind waited for a person. The actions must reach the service
and turn its refusals into a next step the agent can take.
"""
import json

import pytest

from src.agent_tools.worktree_tools import AgentWorktreeTool
from tests.src.agent_worktree.service.test_base_sync import (  # noqa: F401 - fixtures
    cfg, git_required, local_fetch, repo, start_with_change, upstream, upstream_commit,
)

pytestmark = git_required


@pytest.fixture
def tool(cfg, monkeypatch):
    monkeypatch.setattr("src.agent_worktree.config.load_config", lambda: cfg)

    async def call(**args):
        return await AgentWorktreeTool().execute(json.dumps(args), {"owner": "alice"})

    return call


async def test_sync_conflict_then_abort_through_the_tool(cfg, upstream, local_fetch, tool):
    await start_with_change(cfg, "README.md", "one\nbranch\nthree\n")
    upstream_commit(upstream, "README.md", "one\ndev\nthree\n")

    out = await tool(action="sync", name="task")
    assert out["exit_code"] == 0 and out["sync"]["result"] == "conflicted"
    assert out["sync"]["conflicted_paths"] == ["README.md"]

    again = await tool(action="sync", name="task")
    assert again["exit_code"] == 1 and again["code"] == "MERGE_IN_PROGRESS"

    assert (await tool(action="sync", name="task", abort=True))["sync"]["result"] == "aborted"


@pytest.mark.security
async def test_publish_sync_without_an_approval_points_to_request_publish(cfg, upstream, local_fetch, tool):
    await start_with_change(cfg)
    upstream_commit(upstream, "docs/new.md", "dev\n")
    assert (await tool(action="sync", name="task"))["sync"]["result"] == "merged"

    out = await tool(action="publish_sync", name="task")
    assert out["exit_code"] == 1 and out["code"] == "SYNC_NEEDS_APPROVAL"
    assert out["next_action"]["action"] == "request_publish"
