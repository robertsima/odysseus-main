"""python -m tests.run routes each lane to the right selection."""
import pytest

from tests import run


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(run, "pytest", lambda args, parallel=False: seen.append(("pytest", args, parallel)) or 0)
    monkeypatch.setattr(run, "node", lambda files: seen.append(("node", files)) or 0)
    return seen


def _affected_returning(monkeypatch, tests):
    class Fake:
        @staticmethod
        def changed_files(base):
            return ["src/x.py"]

        @staticmethod
        def load_map():
            return {}

        @staticmethod
        def affected(files, coverage_map):
            return tests

    monkeypatch.setattr(run, "_affected_module", lambda: Fake)


def test_full_excludes_browser_and_nightly_and_runs_in_parallel(calls):
    assert run.main(["full", "--", "-x"]) == 0
    kind, args, parallel = calls[0]
    assert args == ["-m", "not browser and not nightly", "-x"] and parallel


def test_affected_falls_back_to_full_when_everything_is_affected(calls, monkeypatch):
    _affected_returning(monkeypatch, None)
    run.main(["affected"])
    assert calls[0][1][:2] == ["-m", "not browser and not nightly"]


def test_affected_runs_nothing_when_no_test_covers_the_change(calls, monkeypatch):
    _affected_returning(monkeypatch, [])
    assert run.main(["affected"]) == 0
    assert calls == []


def test_affected_splits_python_and_node_tests(calls, monkeypatch):
    _affected_returning(monkeypatch, ["tests/a/test_x.py", "tests/static/js/x/x.test.mjs"])
    run.main(["affected"])
    assert calls == [("pytest", ["tests/a/test_x.py", "-m", "not nightly"], False),
                     ("node", ["tests/static/js/x/x.test.mjs"])]


@pytest.mark.parametrize("env,expected", [
    ({}, "4"),
    ({"CI": "true"}, "auto"),
    ({"CI": "true", "ODYSSEUS_TEST_WORKERS": "2"}, "2"),
    ({"ODYSSEUS_TEST_WORKERS": "8"}, "8"),
])
def test_workstations_get_a_bounded_number_of_workers(monkeypatch, env, expected):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ODYSSEUS_TEST_WORKERS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert run.workers() == expected


@pytest.mark.parametrize("lane,marker", [("browser", "browser"), ("security", "security"),
                                         ("nightly", "nightly")])
def test_marker_lanes_select_by_marker(calls, lane, marker):
    run.main([lane])
    assert calls[0][1][:2] == ["-m", marker]
