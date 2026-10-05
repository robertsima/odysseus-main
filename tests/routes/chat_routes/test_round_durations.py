"""An agent reply is saved with the wall time of each of its rounds.

The chat route times every round of an agent turn (a round ends when the
loop starts the next one, or with the turn's metrics) and stores the list in
the reply's metadata as ``round_durations_s``. The page draws a duration
badge from it on reload (static/js/roundTiming.js), so a reply saved without
it loses its timing for good.
"""
from tests.routes.chat_routes.agent_turn import DONE, agent_turn, frame  # noqa: F401  (fixture)


def test_a_two_round_reply_is_saved_with_two_round_durations(agent_turn):
    agent_turn.script = [
        frame({"delta": "Let me look."}),
        frame({"type": "tool_start", "tool": "web_search", "command": "rivers"}),
        frame({"type": "tool_output", "tool": "web_search", "output": "Nile", "exit_code": 0}),
        frame({"type": "agent_step", "round": 2}),
        frame({"delta": "The Nile."}),
        frame({"type": "metrics", "data": {}}),
        DONE,
    ]

    turn = agent_turn.send({"message": "which river is longest", "mode": "agent"})

    durations = agent_turn.saved_replies()[-1]["metadata"]["round_durations_s"]
    assert len(durations) == 2
    assert all(isinstance(d, (int, float)) and d >= 0 for d in durations), durations
    assert '"type": "round_complete", "round": 2' in turn.body
