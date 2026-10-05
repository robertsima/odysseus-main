"""The suite's hygiene rules hold: no new offenders, and the allowlist only shrinks.

See tests/suite/hygiene.py for the rules. When this fails:
- a new offender: fix the test (monkeypatch, a fixture, a behavior assertion);
  add it to hygiene_allowlist.json only with a specific reason;
- a stale allowlist entry: the file was fixed, moved or deleted, so run
  ``python -m tests.suite.hygiene --prune`` and commit the smaller list.
"""
import pytest

from tests.suite import hygiene


@pytest.mark.parametrize("rule", hygiene.RULES)
def test_no_new_offenders(rule):
    new = sorted(hygiene.scan()[rule] - set(hygiene.load_allowlist()[rule]))
    assert not new, f"{rule}: new offenders, see tests/suite/hygiene.py: {new}"


@pytest.mark.parametrize("rule", hygiene.RULES)
def test_allowlist_only_names_current_offenders(rule):
    stale = sorted(set(hygiene.load_allowlist()[rule]) - hygiene.scan()[rule])
    assert not stale, f"{rule}: run `python -m tests.suite.hygiene --prune`; no longer offending: {stale}"


def test_every_allowlist_entry_has_a_reason():
    missing = [(rule, path) for rule, entries in hygiene.load_allowlist().items()
               for path, reason in entries.items() if not reason.strip()]
    assert not missing
