"""Calendar captures keep the person's day and hour in any timezone."""
from datetime import datetime


async def test_an_all_day_event_stays_on_the_persons_date_east_of_utc(cap, env, events):
    """Production bug: a date-only event shifted to the day before for a user at UTC+9."""
    res = await cap("admin", "meeting with the landlord friday", tz_offset=540)

    assert res["status"] == "created"
    [(summary, start, all_day)] = events("admin")
    assert (summary, start.date().isoformat(), all_day) == ("Meeting with the landlord", "2026-10-16", True)


async def test_a_timed_event_is_stored_as_the_same_instant(cap, env, events):
    await cap("admin", "lunch with Sara tomorrow 1pm", tz_offset=540)

    [(summary, start, all_day)] = events("admin")
    assert start == datetime(2026, 10, 11, 4, 0) and all_day is False  # 13:00 at UTC+9 is 04:00 UTC
