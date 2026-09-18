"""A non-admin is not offered tools execution will refuse.

`NON_ADMIN_BLOCKED_TOOLS` was enforced only at execution time
(`tool_execution.is_public_blocked_tool`), so a non-admin agent was shown ~40
schemas it could never call and found out by calling one. That is the phantom
capability the behaviour spec forbids ("Unknown or unavailable capabilities
degrade honestly ... They must not fail later as phantom tools"), it spends
schema tokens on every round, and it is an orchestration trap: a non-admin
agent asked to delegate picks `delegate_to_agent`, is refused, and burns rounds
rediscovering that each turn.

`owner_baseline_disabled_tools` is the shared merge point every agent turn
passes through, whichever route started it, so the blocklist belongs there.
"""

import pytest

from src import tool_security
from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, owner_baseline_disabled_tools


@pytest.fixture
def no_global_config(monkeypatch):
    """Isolate from the operator's own `disabled_tools` setting and privileges."""
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: [])
    monkeypatch.setattr("core.auth.get_auth_manager", lambda: (_ for _ in ()).throw(RuntimeError("no auth")))


def test_a_non_admin_turn_starts_with_the_blocked_tools_already_denied(monkeypatch, no_global_config):
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    denied = owner_baseline_disabled_tools("regular-user")
    for tool in ("bash", "python", "delegate_to_agent", "manage_settings", "send_email"):
        assert tool in denied, tool
    assert NON_ADMIN_BLOCKED_TOOLS <= denied


def test_an_admin_turn_is_not_narrowed_by_it(monkeypatch, no_global_config):
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    denied = owner_baseline_disabled_tools("admin")
    assert not (NON_ADMIN_BLOCKED_TOOLS & denied)


def test_single_user_mode_keeps_every_tool(monkeypatch, no_global_config):
    """Auth disabled is the self-host default; that owner runs their own box."""
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)
    assert not (NON_ADMIN_BLOCKED_TOOLS & owner_baseline_disabled_tools(None))


def test_the_baseline_agrees_with_the_execution_gate(monkeypatch, no_global_config):
    """The two must not disagree: anything execution refuses for this owner has
    to be absent from the schema list, or it is a phantom tool again."""
    from src.tool_execution import is_public_blocked_tool

    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)
    denied = owner_baseline_disabled_tools("regular-user")
    refused_at_execution = {t for t in NON_ADMIN_BLOCKED_TOOLS if is_public_blocked_tool(t)}
    assert refused_at_execution <= denied
