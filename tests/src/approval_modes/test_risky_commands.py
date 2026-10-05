"""Under "Ask for risky actions", a risky shell command asks wherever it sits."""
import pytest

from src.tool_capabilities import ToolRunSecurityContext

pytestmark = pytest.mark.security


def _ask_risky():
    return ToolRunSecurityContext(
        external_untrusted_context_seen=False,
        approval_gate_bypassed=False,
        approval_mode="ask_risky",
    )


@pytest.mark.parametrize("command", [
    "cd repo && git push origin dev",
    "ls; rm -rf build",
    "echo 3 | sudo tee /proc/sys/vm/drop_caches",
    "echo done\ngit push --force",
])
def test_a_risky_command_after_a_harmless_one_still_needs_approval(command):
    decision = _ask_risky().decision_for("bash", command)

    assert not decision.allowed
