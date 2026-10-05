"""Regression coverage for the built-in MCP servers' SDK compatibility line."""

from pathlib import Path
from tests import REPO_ROOT


REQUIREMENTS = REPO_ROOT / "requirements.txt"


def test_mcp_requirement_excludes_breaking_v2_sdk():
    requirements = [
        line.split("#", 1)[0].strip().replace(" ", "")
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    ]

    assert "mcp<2" in requirements
