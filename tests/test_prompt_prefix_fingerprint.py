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


def _fields(line):
    return dict(part.split("=", 1) for part in line.split() if "=" in part)


def _log(caplog, session, payloads):
    llm_core._PREFIX_FINGERPRINTS.pop(session, None)
    with caplog.at_level(logging.INFO, logger="src.llm_core"):
        for payload in payloads:
            llm_core._log_prompt_prefix(session, "m", payload)
    return [_fields(r.getMessage()) for r in caplog.records
            if "[prompt-prefix]" in r.getMessage() and session in r.getMessage()]


def _items(*texts):
    return [{"role": "user", "content": [{"type": "input_text", "text": t}]} for t in texts]


def test_first_diff_item_is_the_append_point_when_history_only_grows(caplog):
    base = {"instructions": "sys", "tools": [{"name": "a"}]}
    lines = _log(caplog, "sess-grow", [
        {**base, "input": _items("u1")},
        {**base, "input": _items("u1") + [{"type": "function_call", "call_id": "c1", "name": "a"}]},
    ])
    assert lines[0]["first_diff_item"] == "-"
    # Pure append: the first new item sits exactly at the previous length.
    assert lines[1]["first_diff_item"] == lines[1]["prev_items"] == "1"
    assert lines[1]["changed"] == "none"


def test_first_diff_item_pins_a_rewrite_in_the_middle_of_the_history(caplog):
    """changed=none with a cache miss: something rewrote an earlier message
    (a ledger batch collapsing a tool result, a moved context envelope). The
    index and kind of the first changed item say which."""
    base = {"instructions": "sys", "tools": [{"name": "a"}]}
    history = _items("u1", "a1") + [
        {"type": "function_call", "call_id": "c1", "name": "a", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "big result " * 50},
    ]
    rewritten = [dict(item) for item in history]
    rewritten[3] = {"type": "function_call_output", "call_id": "c1", "output": "[Execution ledger] short"}
    lines = _log(caplog, "sess-mid", [
        {**base, "input": history},
        {**base, "input": rewritten + _items("u2")},
    ])
    assert lines[1]["changed"] == "none"
    assert lines[1]["first_diff_item"] == "3"
    assert lines[1]["diff_kind"] == "function_call_output"


def test_a_changed_prefix_says_where_it_changed(caplog):
    """Where in the instructions (a character offset, rounded to the hash
    chunk) and which tools came and went -- offsets and tool names, never the
    instruction text."""
    head = "static head " * 100
    lines = _log(caplog, "sess-where", [
        {"instructions": head + "skills: one", "tools": [{"name": "a"}, {"name": "b"}], "input": _items("u1")},
        {"instructions": head + "skills: two", "tools": [{"name": "a"}, {"name": "c"}, {"name": "b"}],
         "input": _items("u1", "u2")},
    ])
    last = lines[1]
    assert last["changed"] == "instructions,tools"
    at, total = (int(x) for x in last["instr_diff_at"].split("/"))
    assert at <= len(head) < at + llm_core._PREFIX_INSTRUCTION_CHUNK and total == len(head) + len("skills: two")
    assert last["tools_diff_at"] == "1"
    assert last["tools_added"] == "c" and "tools_removed" not in last
    assert "skills" not in caplog.text


def test_changed_stays_the_last_field(caplog):
    llm_core._PREFIX_FINGERPRINTS.pop("sess-last", None)
    with caplog.at_level(logging.INFO, logger="src.llm_core"):
        llm_core._log_prompt_prefix("sess-last", "m", _payload())
        llm_core._log_prompt_prefix("sess-last", "m", _payload(instructions="x", items=1))
    lines = [r.getMessage() for r in caplog.records if "sess-last" in r.getMessage()]
    assert lines[-1].rsplit(" ", 1)[1] == "changed=instructions,history_shrank"
