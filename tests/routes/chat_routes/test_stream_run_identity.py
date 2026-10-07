"""What the chat stream tells the browser about the run behind it.

The browser stops and bills a turn by the run id and segment the stream
reports. Without the run id header it cannot stop that exact run, and a
teacher's metrics frame that loses its marker is billed as the primary's.
"""
import json

from tests.routes.chat_routes.agent_turn import DONE, agent_turn, frame  # noqa: F401  (fixture)


def _post(agent_turn):
    return agent_turn.client.post(
        "/api/chat_stream", data={"session": agent_turn.session_id, "message": "hello", "mode": "agent"})


def test_the_stream_names_the_run_it_belongs_to(agent_turn):
    response = _post(agent_turn)

    assert response.status_code == 200
    assert response.headers["X-Odysseus-Run-Id"]
    assert response.headers["X-Agamemnon-Run-Id"] == response.headers["X-Odysseus-Run-Id"]


def test_a_teacher_metrics_frame_keeps_its_marker(agent_turn):
    usage = {"model": "m", "input_tokens": 10, "output_tokens": 5}
    agent_turn.script = [
        frame({"delta": "ok"}),
        frame({"type": "metrics", "data": dict(usage)}),
        frame({"type": "metrics", "teacher": True, "data": dict(usage)}),
        DONE,
    ]

    body = _post(agent_turn).text

    metrics = [json.loads(line[6:]) for line in body.splitlines()
               if line.startswith("data: {") and '"metrics"' in line]
    assert [bool(m.get("teacher")) for m in metrics] == [False, True]
