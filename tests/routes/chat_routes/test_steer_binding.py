"""A steer sent while an agent turn runs reaches that turn's loop.

POST /api/chat_stream gives an agent turn a steering run id before the loop
starts, registers it with the chat's active stream (where the steer route
looks), and hands the same id to the agent loop, which drains the queue under
it between rounds. If either side is missing, the composer's correction is
accepted, queued under another key, and never read.
"""
from routes import chat_routes
from src import agent_activity, agent_control
from tests.routes.chat_routes.agent_turn import DONE, agent_turn, frame  # noqa: F401  (fixture)


def test_a_steer_sent_during_an_agent_turn_is_drained_by_its_loop(agent_turn, monkeypatch, tmp_path):
    # Queueing a steer writes its lifecycle to the activity feed.
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    seen = {}

    async def loop(*args, **kwargs):
        agent_turn._calls.append(kwargs)
        sid = agent_turn.session_id
        seen["steerable"] = agent_control.is_steerable(sid)
        agent_control.steer(sid, "focus on the users table")
        seen["drained"] = agent_control.drain_steer(sid, run_id=kwargs.get("steer_run_id"))
        yield frame({"delta": "Done."})
        yield DONE

    monkeypatch.setattr(chat_routes, "stream_agent_loop", loop)
    try:
        agent_turn.send({"message": "migrate the database", "mode": "agent"})
    finally:
        agent_control._STEER.clear()
        agent_activity._reset_for_tests()

    assert seen == {"steerable": True, "drained": ["focus on the users table"]}
