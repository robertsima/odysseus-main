"""A sandboxed tmux pane is recreated when the workspace gains a worktree.

The pane's bubblewrap binds are fixed when it starts. On 2026-09-28 workers
created their managed worktree mid-run; a pane opened earlier would not have
it bound, so `cd <worktree>` failed. The worktrees bound next to the
workspace are part of the pane's name, so a new one gets a new pane.
"""

from unittest import mock

from src.agent_tools import subprocess_tools


def test_pane_name_changes_when_the_bound_worktrees_change():
    with mock.patch("src.shell_sandbox.workspace_worktrees", return_value=[]):
        before = subprocess_tools._tmux_session_name("chat-1", "/w/umni")
    with mock.patch("src.shell_sandbox.workspace_worktrees", return_value=["/trees/umni/slice"]):
        after = subprocess_tools._tmux_session_name("chat-1", "/w/umni")
        again = subprocess_tools._tmux_session_name("chat-1", "/w/umni")
    assert before != after
    assert after == again


def test_unsandboxed_pane_name_is_unchanged():
    assert subprocess_tools._tmux_session_name("chat-1") == "ody-agent-chat-1"
