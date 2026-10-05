"""Static assets and automatic refetches are not a person using Odysseus.

Production, 2026-09-28: the nightly "Todoist Inbox Prioritization" task was
cancelled at 03:00:46, four Todoist writes in, by
``foreground request GET /static/favicon.ico`` and restarted from scratch at
03:15:46. The same favicon fetch killed "Weekly Todoist Priority Review" at
00:15:46. The chain: another task ("Email Tags") completed, the tab's
/api/tasks/notifications poll picked it up and raised a browser Notification
with ``icon: '/static/favicon.ico'`` (static/js/tasks.js), and the browser's
fetch of that icon went through the foreground gate like a click.

At 00:32:07 the same task died on ``GET /api/calendar/events``. calendar.js
has no timer for it, but it refetches on its own at page boot, on
visibilitychange/focus, on the chat's calendar-refresh event and for
adjacent-month prefetch — none of which is the user asking for events. Those
now carry the poll header; a person really returning to the tab is reported by
the interactive activity heartbeat.
"""
from pathlib import Path

import pytest

from src.interactive_gate import should_track_interactive_request
from tests import REPO_ROOT

ROOT = REPO_ROOT


@pytest.mark.parametrize("path", [
    "/static/favicon.ico",
    "/static/sw.js",
    "/static/manifest.json",
    "/static/js/tasks.js",
    "/static/style.css",
    "/static/icons/icon-192.png",
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/manifest.webmanifest",
    "/robots.txt",
])
def test_asset_fetches_never_preempt_background_work(path):
    assert should_track_interactive_request(path, "GET") is False
    assert should_track_interactive_request(path, "HEAD") is False


@pytest.mark.parametrize("path,method", [
    ("/api/calendar/events", "GET"),
    ("/api/calendar/events", "POST"),
    ("/api/chat_stream", "POST"),
    ("/api/tasks", "POST"),
    ("/", "GET"),
    ("/calendar", "GET"),
])
def test_real_requests_still_count(path, method):
    assert should_track_interactive_request(path, method) is True


def test_a_write_to_a_root_asset_path_is_not_passive():
    # Only GET/HEAD of the conventional root asset paths are exempt.
    assert should_track_interactive_request("/favicon.ico", "POST") is True


def test_the_poll_header_makes_calendar_refetches_passive():
    headers = {"x-odysseus-poll": "1"}
    assert should_track_interactive_request("/api/calendar/events", "GET", headers) is False


def test_an_asset_path_outside_static_does_not_count_as_a_person(monkeypatch):
    """2026-10-02: a stale tab fetched /branding/agamemnon-agent-marks.svg (the
    app serves /static/branding/), and that 404 stopped a scheduled task."""
    import src.interactive_gate as gate

    monkeypatch.setattr(gate, "_enabled", lambda: True)
    assert gate.should_track_interactive_request("/branding/agamemnon-agent-marks.svg", "GET") is False
    assert gate.should_track_interactive_request("/fonts/x.woff2", "GET") is False
    # An API call, or a write, is still a person asking for something.
    assert gate.should_track_interactive_request("/api/files/logo.svg", "GET") is True
    assert gate.should_track_interactive_request("/branding/x.svg", "POST") is True
