"""The delegation provider registry: what `delegation_provider` actually does.

Two things these tests are guarding, beyond the plumbing:

* An explicit provider choice is never silently served by a different one. The
  local CLI exists because it rides a *subscription* instead of metered
  per-token billing, so substituting a vendor is a billing decision, not a
  fallback.
* Availability is a filesystem question, answered without running anything. No
  test here may execute a binary or reach the network — that is also the
  contract the real code must keep, since the probe runs once per model round.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Tuple

import pytest

import src.delegation as delegation
from src.delegation.base import DelegationProvider
from src.delegation.claude_cli import ClaudeCliProvider


class _FakeProvider(DelegationProvider):
    """A provider whose availability and result are dictated by the test."""

    def __init__(self, provider_id: str, ok: bool, detail: str = "", *, raises: bool = False) -> None:
        self.id = provider_id
        self.title = provider_id.replace("_", " ").title()
        self._ok = ok
        self._detail = detail or ("ready" if ok else "not here")
        self._raises = raises
        self.calls: list[tuple[dict, dict]] = []

    def is_available(self) -> Tuple[bool, str]:
        if self._raises:
            raise RuntimeError("probe exploded")
        return self._ok, self._detail

    async def delegate(
        self,
        request: Mapping[str, Any],
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> dict:
        self.calls.append((dict(request), dict(ctx or {})))
        return {"exit_code": 0, "provider": self.id}


@pytest.fixture
def registry(monkeypatch):
    """An empty registry, restored afterwards.

    ``_optional_loaded`` is pinned True so the fixture does not import provider
    modules a build may or may not ship.
    """
    monkeypatch.setattr(delegation, "_REGISTRY", {}, raising=True)
    monkeypatch.setattr(delegation, "_optional_loaded", True, raising=True)
    return delegation


# ── the `delegation_provider` values ──────────────────────────────────────


def test_none_disables_delegation(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=True))
    provider, reason = registry.selection("none")
    assert provider is None
    assert "switched off" in reason


def test_explicit_id_selects_that_provider(registry):
    cli = registry.register(_FakeProvider("claude_code_cli", ok=True, detail="/usr/bin/claude"))
    assert registry.select("claude_code_cli") is cli


def test_explicit_id_that_is_not_installed_reports_so(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=True))
    provider, reason = registry.selection("claude_subscription")
    assert provider is None
    assert "not installed" in reason
    assert "claude_code_cli" in reason  # says what this build does have


def test_explicit_choice_is_never_served_by_another_provider(registry):
    """The point of the setting: an operator who names a provider gets it or a
    reason, not whatever else happens to be installed."""
    registry.register(_FakeProvider("claude_code_cli", ok=False, detail="no binary"))
    other = registry.register(_FakeProvider("mcp", ok=True))
    provider, reason = registry.selection("claude_code_cli")
    assert provider is None
    assert provider is not other
    assert "unusable" in reason and "no binary" in reason


def test_auto_skips_an_unavailable_provider(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=False, detail="no binary at /app/data/claude"))
    subscription = registry.register(_FakeProvider("claude_subscription", ok=True, detail="linked"))
    provider, reason = registry.selection("auto")
    assert provider is subscription
    assert "auto-selected" in reason


def test_auto_takes_the_first_available_in_registration_order(registry):
    first = registry.register(_FakeProvider("claude_code_cli", ok=True))
    registry.register(_FakeProvider("claude_subscription", ok=True))
    assert registry.select("auto") is first


def test_auto_with_nothing_usable_explains_every_provider(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=False, detail="no binary"))
    registry.register(_FakeProvider("mcp", ok=False, detail="no server configured"))
    provider, reason = registry.selection("auto")
    assert provider is None
    assert "no binary" in reason and "no server configured" in reason


def test_a_provider_whose_probe_raises_is_unusable_not_fatal(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=True, raises=True))
    healthy = registry.register(_FakeProvider("mcp", ok=True))
    assert registry.select("auto") is healthy


def test_selection_reads_the_setting_when_no_preference_is_passed(registry, monkeypatch):
    import src.settings as settings

    registry.register(_FakeProvider("claude_code_cli", ok=True))
    chosen = registry.register(_FakeProvider("mcp", ok=True))
    monkeypatch.setattr(
        settings, "get_setting",
        lambda key, default=None: "mcp" if key == "delegation_provider" else default,
    )
    assert registry.select() is chosen


def test_unreadable_settings_fall_back_to_auto(registry, monkeypatch):
    import src.settings as settings

    cli = registry.register(_FakeProvider("claude_code_cli", ok=True))

    def _boom(key, default=None):
        raise OSError("settings.json is gone")

    monkeypatch.setattr(settings, "get_setting", _boom)
    assert registry.select() is cli


def test_availability_lists_every_provider_with_its_reason(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=False, detail="no binary"))
    registry.register(_FakeProvider("mcp", ok=True, detail="ready"))
    assert registry.availability() == [
        ("claude_code_cli", False, "no binary"),
        ("mcp", True, "ready"),
    ]


def test_registering_the_same_id_replaces_it(registry):
    registry.register(_FakeProvider("claude_code_cli", ok=False))
    replacement = registry.register(_FakeProvider("claude_code_cli", ok=True))
    assert registry.get("claude_code_cli") is replacement
    registry.unregister("claude_code_cli")
    assert registry.get("claude_code_cli") is None


def test_the_cli_provider_is_registered_by_default():
    """Whether the CLI *works* is its probe's business; that it is offered as a
    provider is not conditional on import-time host state."""
    assert isinstance(delegation.get("claude_code_cli"), ClaudeCliProvider)


# ── the Claude Code CLI adapter ───────────────────────────────────────────


def _no_binary(monkeypatch, tmp_path):
    """Configured path missing and nothing named `claude` on PATH."""
    import src.agent_tools.claude_code_tools as cc
    import src.delegation.claude_cli as adapter

    monkeypatch.setattr(cc, "binary_path", lambda: tmp_path / "nope" / "claude")
    monkeypatch.setattr(adapter.shutil, "which", lambda name: None)


def test_cli_provider_is_unavailable_without_a_binary(monkeypatch, tmp_path):
    _no_binary(monkeypatch, tmp_path)
    ok, detail = ClaudeCliProvider().is_available()
    assert ok is False
    assert "claude" in detail.lower()
    # The hint must not push the operator onto metered billing.
    assert "no api key is required" in detail.lower()


def test_cli_provider_falls_back_to_path(monkeypatch, tmp_path):
    import src.agent_tools.claude_code_tools as cc
    import src.delegation.claude_cli as adapter

    monkeypatch.setattr(cc, "binary_path", lambda: tmp_path / "nope" / "claude")
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert ClaudeCliProvider().is_available() == (True, "/usr/local/bin/claude")


def test_cli_provider_is_unavailable_when_the_setting_cannot_be_read(monkeypatch):
    import src.agent_tools.claude_code_tools as cc

    def _boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(cc, "binary_path", _boom)
    ok, detail = ClaudeCliProvider().is_available()
    assert ok is False
    assert "RuntimeError" in detail


async def test_delegating_without_a_binary_returns_an_error_instead_of_running(monkeypatch, tmp_path):
    """A failure the model can act on. It also must not reach the CLI at all —
    if it did, the tool layer would be spawning a process that cannot exist."""
    import src.agent_tools.claude_code_tools as cc

    _no_binary(monkeypatch, tmp_path)

    class _Exploding:
        async def execute(self, content, ctx):  # pragma: no cover - must not run
            raise AssertionError("the adapter tried to run a missing binary")

    monkeypatch.setattr(cc, "ClaudeCodeTool", _Exploding)
    result = await ClaudeCliProvider().delegate({"action": "run", "prompt": "fix the build"})
    assert result["exit_code"] == 1
    assert result["provider"] == "claude_code_cli"
    assert "not available" in result["error"]


async def test_delegate_forwards_the_request_to_the_existing_implementation(monkeypatch, tmp_path):
    """Wrap, don't fork: the adapter hands the tool's own argument object to the
    supported entry point and returns its result shape unchanged."""
    import src.agent_tools.claude_code_tools as cc
    import src.delegation.claude_cli as adapter

    monkeypatch.setattr(cc, "binary_path", lambda: tmp_path / "nope" / "claude")
    monkeypatch.setattr(adapter.shutil, "which", lambda name: "/usr/local/bin/claude")

    seen: dict = {}

    class _Recording:
        async def execute(self, content, ctx):
            seen["args"] = json.loads(content)
            seen["ctx"] = ctx
            return {"exit_code": 0, "task_id": "t-1", "repository": "/repo"}

    monkeypatch.setattr(cc, "ClaudeCodeTool", _Recording)
    request = {"action": "start", "prompt": "fix the build", "repository": "/repo"}
    result = await ClaudeCliProvider().delegate(request, {"owner": "rob", "session_id": "s1"})

    assert seen["args"] == request
    assert seen["ctx"] == {"owner": "rob", "session_id": "s1"}
    assert result["task_id"] == "t-1" and result["exit_code"] == 0
    assert result["provider"] == "claude_code_cli"
