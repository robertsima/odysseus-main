"""src.tool_utils pulls in no project module beyond src.constants.

The module exists to break a circular import. If importing it drags in
src.settings, src.database or the tool registry, the cycle returns and the
importer gets a partially initialized module.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _project_modules_after(statement):
    code = (
        "import sys, json;" + statement + ";"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m == 'core' or m.startswith(('src.', 'core.', 'routes.', 'services.')))))"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "DATABASE_URL": "sqlite:///:memory:"}
    result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr[-1500:]
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def test_importing_tool_utils_loads_nothing_beyond_src_constants():
    baseline = _project_modules_after("import src.constants")

    loaded = _project_modules_after("import src.tool_utils")

    assert loaded - baseline <= {"src.tool_utils"}, sorted(loaded - baseline)
