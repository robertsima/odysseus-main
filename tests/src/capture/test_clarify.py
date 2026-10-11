"""Ask only when two destinations are both plausible, and write nothing until answered."""


async def test_ambiguous_line_writes_nothing_then_the_answer_writes_once(cap, env, events, ledger):
    """Production bug: 'dentist friday' guessed one destination and the guess was wrong half the time."""
    first = await cap("admin", "dentist friday", idempotency_key="k1")

    assert first["status"] == "needs_clarification"
    assert {o["destination"] for o in first["options"]} == {"calendar", "todoist"}
    assert first["question"]
    assert env.td.calls == [] and events("admin") == [] and env.notes.notes == {}
    assert env.outcomes.list_for("admin") == [] and ledger() == []

    task = next(o for o in first["options"] if o["destination"] == "todoist")
    second = await cap("admin", "dentist friday", idempotency_key="k1", destination="todoist", due=task.get("due"))
    again = await cap("admin", "dentist friday", idempotency_key="k1", destination="todoist", due=task.get("due"))

    assert (second["status"], second["destination"]) == ("created", "todoist")
    assert env.td.creates == 1 and again["ref"] == second["ref"]
    assert ledger() == [("admin", "k1", "done")]


async def test_a_bare_appointment_word_is_ambiguous_for_a_non_admin_too(cap, env):
    res = await cap("alice", "dentist")

    assert res["status"] == "needs_clarification"
    assert {o["destination"] for o in res["options"]} == {"calendar", "note"}


async def test_choosing_the_calendar_with_a_date_makes_an_all_day_event(cap, env, events):
    first = await cap("admin", "dentist friday")
    cal = next(o for o in first["options"] if o["destination"] == "calendar")

    res = await cap("admin", "dentist friday", destination="calendar", due=cal.get("due"))

    assert res["status"] == "created"
    [(summary, start, all_day)] = events("admin")
    assert (summary, start.date().isoformat(), all_day) == ("Dentist", "2026-10-16", True)


async def test_the_calendar_without_any_date_fails_instead_of_inventing_one(cap, env, events, ledger):
    env.model.replies.append('{"summary": "Dentist", "dtstart": "", "all_day": false}')

    res = await cap("admin", "dentist", destination="calendar")

    assert res["status"] == "failed" and "date" in res["error"]
    assert events("admin") == []


async def test_a_low_confidence_model_answer_with_an_alternative_asks(cap, env):
    import json
    env.model.replies.append(json.dumps({"destination": "calendar", "confidence": 0.4, "alternative": "task"}))

    res = await cap("admin", "kombucha starter")

    assert res["status"] == "needs_clarification"
    assert {o["destination"] for o in res["options"]} == {"calendar", "todoist"}
    assert env.td.calls == []
