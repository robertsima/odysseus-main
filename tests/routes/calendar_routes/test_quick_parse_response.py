"""POST /api/calendar/quick-parse keeps its body, response shape and tz handling."""
import json

import pytest

import src.llm_core
import src.user_time


@pytest.fixture
def model(api, monkeypatch):
    seen = {}

    async def fake_llm_call_async(url, model, messages, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return "```json\n" + json.dumps({
            "summary": "Lunch with Sara at 1pm",
            "dtstart": "2026-10-09T13:00:00Z", "dtend": "",
            "all_day": False, "location": "Downtown", "confidence": 0.9,
        }) + "\n```"

    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://model.test/v1", "m", {}))
    return seen


def test_quick_parse_returns_the_structured_event_and_reads_the_body_tz(api, model):
    """Bug caught: moving the parse into a helper drops tz from the prompt or the response fields."""
    res = api.as_user("alice").post("/api/calendar/quick-parse", json={
        "text": "lunch with sara friday 1pm", "tz": "Asia/Tokyo", "tz_offset": 540})

    assert res.status_code == 200
    assert res.json() == {"ok": True, "confidence": 0.9, "event": {
        "summary": "Lunch with Sara at", "dtstart": "2026-10-09T13:00:00",
        "dtend": "2026-10-09T14:00:00", "all_day": False,
        "location": "Downtown", "description": ""}}
    assert model["user"] == "lunch with sara friday 1pm"
    assert "Asia/Tokyo" in model["system"]


def test_quick_parse_rejects_empty_text(api, model):
    res = api.as_user("alice").post("/api/calendar/quick-parse", json={"text": "  "})

    assert res.status_code == 400
