"""The CI gate: pytest runs on every push to dev and on PRs, and a red run is red.

Parses .github/workflows/ci.yml instead of matching its text, so reformatting
the workflow or adding jobs does not trip these tests; dropping the dev trigger
or making the pytest job advisory does.
"""

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def _pytest_steps(job: dict) -> list[str]:
    return [s["run"] for s in job.get("steps", []) if "python -m pytest" in str(s.get("run", ""))]


def test_ci_runs_on_pushes_to_dev_and_on_pull_requests():
    triggers = _workflow()["on"]
    push = triggers["push"]
    assert "dev" in push["branches"]
    assert "paths-ignore" not in push and "paths" not in push
    assert "pull_request" in triggers


def test_python_tests_run_the_suite_and_can_fail_the_run():
    job = _workflow()["jobs"]["python-tests"]
    assert not job.get("continue-on-error")
    steps = _pytest_steps(job)
    assert steps, "python-tests has no pytest step"
    assert all("|| true" not in run for run in steps)


def test_browser_tests_run_in_their_own_job_that_can_fail_the_run():
    jobs = _workflow()["jobs"]
    browser = [name for name, job in jobs.items() if any("-m browser" in run for run in _pytest_steps(job))]
    assert browser, "no job runs the browser marker"
    assert all(not jobs[name].get("continue-on-error") for name in browser)
    assert all("not browser" in run for run in _pytest_steps(jobs["python-tests"]))
