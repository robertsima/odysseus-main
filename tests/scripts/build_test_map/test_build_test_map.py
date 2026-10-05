"""scripts/build_test_map.py maps production files to the test files that ran them."""
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "build_test_map.py"


@pytest.fixture(scope="module")
def btm():
    spec = importlib.util.spec_from_file_location("build_test_map_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("context,test_file", [
    ("tests/src/agent_loop/test_x.py::test_y|run", "tests/src/agent_loop/test_x.py"),
    ("tests/test_a.py::TestB::test_c[1]|setup", "tests/test_a.py"),
    ("", None),
    ("not-a-test-context", None),
])
def test_contexts_reduce_to_their_test_file(btm, context, test_file):
    assert btm.test_file_of(context) == test_file


def test_build_maps_measured_production_files_to_test_files(btm, tmp_path):
    coverage = pytest.importorskip("coverage")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    prod = str(_ROOT / "src" / "agent_loop.py")
    helper = str(_ROOT / "tests" / "helpers" / "sqlite_db.py")
    data.set_context("tests/test_a.py::test_one|run")
    data.add_lines({prod: [1, 2], helper: [1]})
    data.set_context("tests/test_b.py::test_two|run")
    data.add_lines({prod: [3]})
    data.write()
    files = btm.build(str(tmp_path / ".coverage"))
    assert files == {"src/agent_loop.py": ["tests/test_a.py", "tests/test_b.py"]}
