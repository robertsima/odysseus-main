"""scripts/affected_tests.py picks the tests that cover a change."""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "affected_tests.py"


@pytest.fixture(scope="module")
def at():
    spec = importlib.util.spec_from_file_location("affected_tests_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/plugins/network_guard.py",
                                  "tests/helpers/sqlite_db.py", "pyproject.toml", "requirements.txt"])
def test_infrastructure_changes_select_everything(at, path):
    assert at.affected([path], {}) is None


def test_docs_changes_select_nothing(at):
    assert at.affected(["website/testing-restructure-2026-10-03.md"], {}) == []


def test_a_changed_test_selects_itself(at):
    assert at.affected(["tests/suite/test_hygiene.py"], {}) == ["tests/suite/test_hygiene.py"]


def test_a_mapped_file_selects_its_existing_mapped_tests(at):
    coverage_map = {"src/agent_loop.py": ["tests/suite/test_hygiene.py", "tests/gone/test_deleted.py"]}
    selected = at.affected(["src/agent_loop.py"], coverage_map)
    assert "tests/suite/test_hygiene.py" in selected
    assert "tests/gone/test_deleted.py" not in selected


def test_the_mirrored_folder_is_included(at):
    selected = at.affected([".github/scripts/red_evidence.py"], {})
    assert "tests/github/scripts/red_evidence/test_red_evidence.py" in selected


@pytest.mark.parametrize("path,folder", [
    ("src/agent_loop.py", "tests/src/agent_loop"),
    ("routes/document/document_routes.py", "tests/routes/document/document_routes"),
    ("static/js/chat.js", "tests/static/js/chat"),
    (".github/scripts/red_evidence.py", "tests/github/scripts/red_evidence"),
])
def test_mirror_folder_follows_the_production_path(at, path, folder):
    assert at.mirror_folder(path) == folder


def test_an_unmapped_file_falls_back_to_tests_that_mention_it(at):
    selected = at.affected(["scripts/affected_tests.py"], {})
    assert "tests/scripts/affected_tests/test_affected_tests.py" in selected
