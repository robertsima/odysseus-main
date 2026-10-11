"""The deterministic rules: which line goes where, and which dates they read."""
from datetime import datetime

import pytest

from src import capture_rules as rules

NOW = datetime(2026, 10, 10, 10, 0)  # Saturday, local wall clock


@pytest.mark.parametrize("text,route", [
    ("Lunch with Sara tomorrow 1pm", "calendar"),
    ("meeting with the landlord friday at 3", "calendar"),
    ("call with Priya tomorrow 9:30am", "calendar"),
    ("dentist tomorrow 4pm", "calendar"),
    ("dentist friday", "clarify"),
    ("dentist", "clarify"),
    ("lunch with Sara", "clarify"),
    ("call the dentist friday", "task"),
    ("book dentist appointment", "task"),
    ("pay the electricity bill friday", "task"),
    ("remind me to call mom at 5pm", "task"),
    ("add a task to renew passport", "task"),
    ("don't forget to buy milk", "task"),
    ("waiting on Sam for the contract", "waiting"),
    ("follow up with Dr. Lee about the results friday", "waiting"),
    ("I'm waiting for the plumber", "waiting"),
    ("plan the offsite: book venue, then send invites", "outcome"),
    ("kombucha starter", None),
    ("plan the offsite", None),
])
def test_rules_pick_a_route(text, route):
    assert rules.classify(text, NOW).route == route


def test_the_waiting_person_is_cut_at_the_first_connective():
    d = rules.classify("waiting on Sam for the signed contract, follow up friday", NOW)

    assert d.waiting_on == "Sam" and d.when.start == datetime(2026, 10, 16, 0, 0)


def test_a_reminder_with_a_time_is_flagged_as_a_reminder_and_the_prefix_is_dropped():
    d = rules.classify("remind me to call mom at 5pm", NOW)

    assert d.reminder is True and d.title == "Call mom"
    assert d.when.start == datetime(2026, 10, 10, 17, 0) and d.when.has_time


@pytest.mark.parametrize("phrase,start,has_time", [
    ("tomorrow", datetime(2026, 10, 11), False),
    ("friday", datetime(2026, 10, 16), False),
    ("next monday", datetime(2026, 10, 12), False),
    ("tomorrow at 9am", datetime(2026, 10, 11, 9), True),
    ("5pm", datetime(2026, 10, 10, 17), True),
    ("9am", datetime(2026, 10, 11, 9), True),  # already past today
    ("in 3 days", datetime(2026, 10, 13), False),
    ("in 2 hours", datetime(2026, 10, 10, 12), True),
    ("2026-12-01", datetime(2026, 12, 1), False),
    ("2026-10-16T15:30:00", datetime(2026, 10, 16, 15, 30), True),
    ("oct 20", datetime(2026, 10, 20), False),
    ("jan 5", datetime(2027, 1, 5), False),
    ("next week", datetime(2026, 10, 12), False),
])
def test_date_phrases_resolve_against_the_persons_clock(phrase, start, has_time):
    when = rules.resolve_phrase(phrase, NOW)

    assert (when.start, when.has_time) == (start, has_time)


def test_a_date_phrase_inside_a_word_is_not_a_date():
    assert rules.strip_when("Submit the thesis on satisfaction surveys", NOW)[1] is None


@pytest.mark.parametrize("a,b", [
    ("Renew passport friday", "Renew passport"),
    ("Renew passport 2026-10-16 @waiting", "renew   passport"),
    ("Renew passport @errands p2 #Admin", "Renew passport"),
    ("Remind me to renew passport by next friday", "Renew passport"),
    ("Pay rent tomorrow at 9am", "pay rent"),
])
def test_normalised_titles_match(a, b):
    assert rules.normalize_title(a) == rules.normalize_title(b)


def test_different_tasks_do_not_normalise_together():
    assert rules.normalize_title("Renew passport") != rules.normalize_title("Renew licence")
