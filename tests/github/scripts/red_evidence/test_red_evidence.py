"""The red-evidence check picks the right tests and judges them correctly."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[4] / ".github" / "scripts" / "red_evidence.py"


@pytest.fixture(scope="module")
def red():
    name = "red_evidence_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # @dataclass resolves its module while the class is built
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


def test_a_renamed_test_keeps_its_body_hash(red):
    before = red.test_functions("def test_a():\n    assert f() == 1\n")
    after = red.test_functions("def test_b():\n    assert f() == 1\n")
    assert before["test_a"] == after["test_b"]


def test_a_changed_assertion_changes_the_hash(red):
    before = red.test_functions("def test_a():\n    assert f() == 1\n")
    after = red.test_functions("def test_a():\n    assert f() == 2\n")
    assert before["test_a"] != after["test_a"]


def test_class_methods_get_class_qualified_ids(red):
    found = red.test_functions("class TestX:\n    def test_y(self):\n        pass\n    def helper(self):\n        pass\n")
    assert list(found) == ["TestX::test_y"]


@pytest.mark.parametrize("paths,expected", [
    (["src/agent_loop.py", "tests/test_x.py"], True),
    (["app.py"], True),
    (["static/js/chat.js"], True),
    (["tests/test_x.py", "website/plan.md", "README.md"], False),
    ([".github/workflows/ci.yml"], False),
])
def test_only_production_changes_need_red_evidence(red, paths, expected):
    assert red.touches_production(paths) is expected


def test_summary_folds_parametrized_cases_into_the_worst_outcome(red):
    out = "PASSED tests/t.py::test_a[1]\nFAILED tests/t.py::test_a[2] - assert 1 == 2\nPASSED tests/t.py::test_b\n"
    assert red.parse_summary(out, ["tests/t.py::test_a", "tests/t.py::test_b"]) == {
        "tests/t.py::test_a": "failed", "tests/t.py::test_b": "passed"}


def test_a_collection_error_marks_the_files_tests_as_errors(red):
    out = "ERROR tests/t.py - ModuleNotFoundError: No module named 'src.new_thing'\n"
    assert red.parse_summary(out, ["tests/t.py::test_a"]) == {"tests/t.py::test_a": "error"}


def test_a_test_never_reported_counts_as_an_error(red):
    assert red.parse_summary("", ["tests/t.py::test_a"]) == {"tests/t.py::test_a": "error"}


@pytest.mark.parametrize("base,head,good", [
    ("failed", "passed", True),
    ("error", "passed", True),
    ("passed", "passed", False),
    ("skipped", "passed", False),
    ("failed", "failed", False),
])
def test_only_red_on_base_and_green_on_head_counts(red, base, head, good):
    assert red.verdict(base, head).startswith("ok") is good


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_new_and_changed_tests_are_picked_and_moved_ones_are_not(red, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "tests" / "test_old.py").write_text(
        "def test_kept():\n    assert 1\n\ndef test_edited():\n    assert 1\n\ndef test_moving():\n    assert 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

    (repo / "tests" / "test_old.py").write_text("def test_kept():\n    assert 1\n\ndef test_edited():\n    assert 2\n")
    (repo / "tests" / "test_new.py").write_text("def test_renamed_move():\n    assert 3\n\ndef test_fresh():\n    assert 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "head")

    monkeypatch.chdir(repo)
    rows = red.changed_files(base, "HEAD")
    picked = red.new_or_changed_tests(base, "HEAD", rows)
    assert sorted(picked.python) == ["tests/test_new.py::test_fresh", "tests/test_old.py::test_edited"]
    assert picked.moved == 1
