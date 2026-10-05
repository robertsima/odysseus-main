"""A stopped agent turn keeps its tool calls (src/turn_trail.py).

On 2026-09-29 a 27-round run was replaced by the user's next message. Only
its visible text was saved: the tool cards were gone after a reload and the
next turn could not tell that a bash command had been running.
"""
from src.turn_trail import TurnTrail


def _feed(trail, events, now=0.0):
    for ev in events:
        if "delta" in ev:
            trail.text(ev["delta"])
        else:
            trail.event(ev, now=now)


def test_stopped_while_a_tool_runs():
    trail = TurnTrail()
    _feed(trail, [
        {"delta": "I found the backlog item."},
        {"type": "tool_start", "tool": "read_file", "command": "a.py"},
        {"type": "tool_output", "tool": "read_file", "command": "a.py", "output": "code", "exit_code": 0},
        {"type": "agent_step", "round": 2},
        {"type": "tool_start", "tool": "bash", "command": "cd mobile && npm test"},
        {"type": "tool_progress", "tool": "bash", "tail": "PASS a.test.ts"},
    ], now=100.0)

    note, events, texts = trail.stopped_record(now=100.0 + 23 * 60 + 5)

    assert note == ("[Turn stopped while `bash` had been running for 23 min: "
                    "`cd mobile && npm test`. 1 tool call had finished.]")
    assert events[0] == {"round": 1, "tool": "read_file", "command": "a.py", "output": "code", "exit_code": 0}
    assert events[1] == {"round": 2, "tool": "bash", "command": "cd mobile && npm test",
                         "output": "PASS a.test.ts", "exit_code": None, "stopped": True}
    # Round texts line up with the tool rounds; the note renders last.
    assert texts == ["I found the backlog item.", "", note]


def test_parallel_tools_are_matched_by_name():
    trail = TurnTrail()
    _feed(trail, [
        {"type": "tool_start", "tool": "grep", "command": "a"},
        {"type": "tool_start", "tool": "read_file", "command": "b"},
        {"type": "tool_output", "tool": "read_file", "command": "b", "output": "", "exit_code": 0},
    ])
    note, events, _ = trail.stopped_record(now=5)
    assert [e["tool"] for e in events] == ["read_file", "grep"]
    assert events[1]["stopped"] is True
    assert note == "[Turn stopped while `grep` had been running for 5s: `a`. 1 tool call had finished.]"


def test_stopped_between_tools_and_text_only():
    trail = TurnTrail()
    _feed(trail, [
        {"type": "tool_start", "tool": "ls", "command": "."},
        {"type": "tool_output", "tool": "ls", "output": "x", "exit_code": 0},
        {"type": "tool_start", "tool": "ls", "command": "src"},
        {"type": "tool_output", "tool": "ls", "output": "y", "exit_code": 0},
    ])
    note, events, _ = trail.stopped_record()
    assert note == "[Turn stopped. 2 tool calls had finished.]"
    assert len(events) == 2

    plain = TurnTrail()
    plain.text("just words")
    assert plain.stopped_record() == ("", [], ["just words"])
    assert not plain.has_work()


def test_command_backticks_do_not_break_the_note():
    trail = TurnTrail()
    trail.event({"type": "tool_start", "tool": "bash", "command": "echo `date`"}, now=0)
    note, _, _ = trail.stopped_record(now=1)
    assert note == "[Turn stopped while `bash` had been running for 1s: `echo 'date'`.]"
