"""A hand-edited, non-numeric agent_max_tool_calls must not break the stream.

The settings endpoint validates the value, but settings.json can be edited
directly. An unguarded int() inside the agent stream raised ValueError past a
handler that only catches cancellation, and the chat broke mid-request.
"""
import json

from tests.routes.chat_routes.agent_turn import agent_turn  # noqa: F401  (fixture)


def _use_settings(monkeypatch, tmp_path, values):
    import src.settings as settings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(path))
    monkeypatch.setattr(settings, "_settings_cache", None)


def test_a_non_numeric_budget_runs_the_turn_without_a_limit(agent_turn, monkeypatch, tmp_path):
    _use_settings(monkeypatch, tmp_path, {"agent_max_tool_calls": "unlimited"})

    turn = agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert '"delta": "Done."' in turn.body
    assert turn.loop_kwargs["max_tool_calls"] == 0


def test_a_numeric_budget_reaches_the_loop_with_its_margin(agent_turn, monkeypatch, tmp_path):
    _use_settings(monkeypatch, tmp_path, {"agent_max_tool_calls": 200})

    turn = agent_turn.send({"message": "list the files here", "mode": "agent"})

    # The hard stop sits a margin above the budget so the turn gets its
    # closing answer first.
    assert turn.loop_kwargs["max_tool_calls"] == 220
