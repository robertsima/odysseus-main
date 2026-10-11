"""Hand-back parsing and the evidence rule: a worker saying "done" is not proof."""
from src.handback import evidence, parse_handback

BOLD_PARTIAL = """\
**Outcome: partly done.** The importer handles CSV but not the XLSX path.

**Changed:** src/importer.py, tests/src/importer/test_csv.py; commit a1b2c3d on agent/importer
**Checked:** ran pytest tests/src/importer -q: 12 passed
**Open:**
- XLSX sheets with merged cells
- decide whether to keep the legacy `--strict` flag
"""

BLOCKED = """\
Outcome: blocked, the staging database refuses my login.
Changed: nothing.
Checked: could not run the migration.
Open: the migration for 0042.
Needs parent: grant me the staging read-only DSN
"""

DONE_WITH_COUNT = """\
Outcome: done
Changed: src/parser.py (date handling), commit 9f8e7d6
Checked: python -m pytest tests/src/parser -q -> 129 passed in 14s
"""

DONE_NO_FACTS = """\
Outcome: done
Changed: updated the parser so dates work.
Checked: looked it over and it works.
"""


def test_bold_partly_done_is_partial_with_lists():
    hb = parse_handback(BOLD_PARTIAL)

    assert hb.outcome == "partial"
    assert hb.changed == ["src/importer.py, tests/src/importer/test_csv.py; commit a1b2c3d on agent/importer"]
    assert hb.checked == ["ran pytest tests/src/importer -q: 12 passed"]
    assert hb.open == ["XLSX sheets with merged cells", "decide whether to keep the legacy `--strict` flag"]
    assert hb.needs_user == [] and hb.needs_parent == []


def test_blocked_with_a_needs_parent_line():
    hb = parse_handback(BLOCKED)

    assert hb.outcome == "blocked"
    assert hb.needs_parent == ["grant me the staging read-only DSN"]
    assert hb.needs_user == []
    assert hb.open == ["the migration for 0042."]


def test_synonyms_for_done():
    assert parse_handback("Outcome: complete").outcome == "done"
    assert parse_handback("**Outcome:** Completed, all parts met").outcome == "done"
    assert parse_handback("- Outcome: partially done").outcome == "partial"


def test_the_prompts_own_template_line_is_not_an_outcome():
    """A worker that echoes `done, partly done or blocked` must not be read as done."""
    hb = parse_handback("Outcome: done, partly done or blocked, and why.")

    assert hb.outcome is None


def test_text_with_no_labelled_lines_has_no_outcome():
    hb = parse_handback("I looked into it and everything is fine.")

    assert hb.outcome is None and hb.changed == [] and hb.checked == []


def test_done_with_a_passed_count_and_a_commit_yields_tests_and_commit_evidence():
    hb = parse_handback(DONE_WITH_COUNT)

    items = evidence(hb, {"commit": "9f8e7d6aaa", "branch": "agent/parser"}, by="agent:w1")

    kinds = {item["kind"]: item for item in items}
    assert set(kinds) == {"commit", "tests"}
    assert kinds["commit"]["ref"] == "9f8e7d6aaa"
    assert "129 passed" in kinds["tests"]["detail"]
    assert all(item["by"] == "agent:w1" and item["at"] for item in items)


def test_a_passed_count_alone_counts_as_tests_evidence():
    """Claims without a run fact count only when the Checked line states a passed count."""
    items = evidence(parse_handback(DONE_WITH_COUNT), {})

    assert [item["kind"] for item in items] == ["tests"]


def test_done_claim_with_no_facts_yields_no_evidence():
    """Production bug: a worker said done, nothing backed it, the system called it complete."""
    hb = parse_handback(DONE_NO_FACTS)

    assert hb.outcome == "done"
    assert evidence(hb, {}) == []
    assert evidence(hb, {"changed_files": [], "commits": 0, "commit": None}) == []


def test_failures_in_the_checked_line_void_the_passed_count():
    hb = parse_handback("Outcome: done\nChecked: pytest: 129 passed, 3 failed")

    assert evidence(hb, {}) == []


def test_zero_passed_is_not_evidence():
    hb = parse_handback("Outcome: done\nChecked: 0 passed, no tests collected")

    assert evidence(hb, {}) == []


def test_run_facts_become_evidence_without_any_checked_text():
    hb = parse_handback("Outcome: done\nChanged: the importer")
    run = {
        "commits": [{"short": "abc1234", "subject": "fix importer"}, "def5678"],
        "pull_request": {"number": 7, "url": "https://github.com/o/r/pull/7"},
        "changed_files": ["src/importer.py", "tests/test_importer.py"],
    }

    items = evidence(hb, run, run_id="run-1")

    by_kind = {}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)
    assert [i["ref"] for i in by_kind["commit"]] == ["abc1234", "def5678"]
    assert by_kind["pull_request"][0]["ref"] == "https://github.com/o/r/pull/7"
    assert by_kind["run"][0]["ref"] == "run-1"
    assert "2 files" in by_kind["run"][0]["detail"]


def test_a_commit_count_without_a_sha_is_not_evidence():
    """claude_code runs report `commits` as a bare count; a number cannot be checked."""
    hb = parse_handback("Outcome: done")

    assert evidence(hb, {"commits": 3}) == []
