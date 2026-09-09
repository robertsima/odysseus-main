"""Any module in the tool pipeline must be importable first.

The tool registry, the fence parser and the schema list refer to each other. For
a long time that only resolved when the cluster was entered through
``src.agent_tools``; importing ``src.tool_schemas`` first raised
``ImportError: cannot import name 'FUNCTION_TOOL_SCHEMAS' from partially
initialized module`` and took the whole application down at startup, because
uvicorn could not import ``app``.

Each module is imported in its own interpreter, because once one of them is in
``sys.modules`` the cycle is already resolved and the bug becomes invisible.
That is exactly why in-process tests kept passing while the container would not
boot.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.area_unit

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every module that participates in the tool-pipeline import cluster, plus the
# entry points that reach it from outside.
CLUSTER_MODULES = [
    "src.tool_types",
    "src.tool_security",
    "src.tool_parsing",
    "src.tool_schemas",
    "src.tool_execution",
    "src.agent_tools",
    "src.agent_loop",
]


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(REPO_ROOT),
            "DATABASE_URL": "sqlite:///:memory:",
            "PYTHONPATH": str(REPO_ROOT),
        },
    )


@pytest.mark.parametrize("module", CLUSTER_MODULES)
def test_module_imports_as_the_entry_point(module):
    result = _import_in_fresh_interpreter(module)
    assert result.returncode == 0, (
        f"importing {module} first fails, which breaks application startup:\n"
        f"{result.stderr[-2000:]}"
    )


@pytest.mark.parametrize("module", CLUSTER_MODULES)
def test_no_circular_import_error_is_reported(module):
    """A clearer failure message for the specific symptom seen in production."""
    result = _import_in_fresh_interpreter(module)
    assert "partially initialized module" not in result.stderr, (
        f"circular import triggered by importing {module} first:\n{result.stderr[-2000:]}"
    )


def test_tool_types_stays_a_leaf():
    """The fix only holds while tool_types imports nothing from the cluster.

    If a future change makes it import the registry, parsing or schemas, the
    cycle comes back and the app stops booting.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "src" / "tool_types.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {"src.agent_tools", "src.tool_parsing", "src.tool_schemas",
                 "src.tool_execution"}
    offending = sorted(imported & forbidden)
    assert not offending, (
        f"src/tool_types.py must not depend on {offending}; it is the leaf that "
        "breaks the tool-pipeline import cycle"
    )


def test_the_reexports_are_the_same_objects():
    """Existing importers use src.agent_tools; they must get the real objects."""
    from src import agent_tools, tool_parsing, tool_schemas, tool_types

    assert agent_tools.ToolBlock is tool_types.ToolBlock
    assert agent_tools.TOOL_TAGS is tool_types.TOOL_TAGS
    assert tool_parsing.ToolBlock is tool_types.ToolBlock
    assert tool_schemas.TOOL_TAGS is tool_types.TOOL_TAGS
