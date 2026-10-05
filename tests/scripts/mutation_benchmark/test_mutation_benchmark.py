"""scripts/mutation_benchmark.py reads results right and its expectations match its patches."""
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "mutation_benchmark.py"
_MUTANTS = _ROOT / "tests" / "mutants"


@pytest.fixture(scope="module")
def mb():
    spec = importlib.util.spec_from_file_location("mutation_benchmark_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failing_tests_reads_failed_and_error_lines(mb):
    out = ("FAILED tests/a.py::test_x - assert 1 == 2\nERROR tests/b.py - ImportError\n"
           "PASSED tests/c.py::test_y\n3 failed, 10 passed\n")
    assert mb.failing_tests(out) == {"tests/a.py::test_x", "tests/b.py"}


@pytest.mark.parametrize("pid,new,result", [
    ("B01", {"tests/a.py::t"}, "caught"),
    ("B01", set(), "missed"),
    ("N01", set(), "clean"),
    ("N01", {"tests/a.py::t"}, "flagged"),
])
def test_outcome_depends_on_patch_kind(mb, pid, new, result):
    assert mb.outcome(pid, new) == result


def test_every_patch_has_an_expectation_and_every_expectation_a_patch():
    expected = json.loads((_MUTANTS / "expected.json").read_text(encoding="utf-8"))
    patches = {p.stem for p in _MUTANTS.glob("*.patch")}
    assert set(expected) == patches
    assert all(v["expect"] == ("caught" if k.startswith("B") else "clean") for k, v in expected.items())
