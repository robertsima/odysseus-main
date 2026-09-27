"""The per-request prompt-prefix fingerprint names what changed between a
session's requests, so a cross-turn prompt-cache miss can be traced."""

import logging

from src import llm_core


def _payload(instructions="sys", tools=("a",), items=2):
    return {
        "instructions": instructions,
        "tools": [{"name": name} for name in tools],
        "input": [{"role": "user", "content": str(i)} for i in range(items)],
    }


def test_fingerprint_names_the_changed_part(caplog):
    llm_core._PREFIX_FINGERPRINTS.pop("sess-fp", None)
    with caplog.at_level(logging.INFO, logger="src.llm_core"):
        llm_core._log_prompt_prefix("sess-fp", "m", _payload())
        llm_core._log_prompt_prefix("sess-fp", "m", _payload(items=3))
        llm_core._log_prompt_prefix("sess-fp", "m", _payload(instructions="sys2", items=4))
        llm_core._log_prompt_prefix("sess-fp", "m", _payload(instructions="sys2", tools=("a", "b"), items=1))
    lines = [r.getMessage() for r in caplog.records if "[prompt-prefix]" in r.getMessage()]
    assert [line.rsplit("changed=", 1)[1] for line in lines] == [
        "first", "none", "instructions", "tools,history_shrank",
    ]


def test_fingerprint_logs_hashes_not_content(caplog):
    llm_core._PREFIX_FINGERPRINTS.pop("sess-secret", None)
    with caplog.at_level(logging.INFO, logger="src.llm_core"):
        llm_core._log_prompt_prefix("sess-secret", "m", _payload(instructions="my private vault note"))
    assert "private vault" not in caplog.text


def test_fingerprint_table_is_bounded():
    for i in range(llm_core._PREFIX_FINGERPRINTS_MAX + 20):
        llm_core._log_prompt_prefix(f"bounded-{i}", "m", _payload())
    assert len(llm_core._PREFIX_FINGERPRINTS) <= llm_core._PREFIX_FINGERPRINTS_MAX
