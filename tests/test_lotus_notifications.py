"""Lotus notification delivery, insights, and the model-facing wellbeing tool."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.lotus_routes import setup_lotus_routes
from src.auth_helpers import require_user
from src.lotus_checkins import LotusCheckinStore, owner_has_lotus_data, owner_storage_key
from src.lotus_insights import compute_insights, render_observations
from src.lotus_notifications import (
    Notification,
    due_notifications,
    in_quiet_hours,
    resolved_channel,
    settings_override_for,
)

NOTE_TEXT = "A private synthetic note that must never leave the database."


def _base_prefs(**overrides):
    prefs = {
        "timezone": "UTC",
        "reminder_enabled": True,
        "reminder_times": ["20:00"],
        "reminder_weekdays": [0, 1, 2, 3, 4, 5, 6],
        "quiet_start": None,
        "quiet_end": None,
        "snooze_minutes": 30,
        "channel": "inherit",
        "min_hours_between": 0,
        "skip_if_checked_in": False,
        "message_style": "plain",
        "paused_until": None,
        "insights_enabled": False,
        "insights_frequency": "weekly",
        "insights_weekday": 6,
        "insights_time": "09:00",
        "_owner_key": "ownerhash",
    }
    prefs.update(overrides)
    return prefs


def _checkin(store, *, offset_hours=0, emotion="calm", energy=0.4, note=NOTE_TEXT, base=None):
    moment = (base or datetime(2026, 8, 3, 9, 0, tzinfo=UTC)) + timedelta(hours=offset_hours)
    return store.create_checkin(
        {
            "occurred_at": moment,
            "timezone": "UTC",
            "emotion_label": emotion,
            "emotion_family": "pleasant_low",
            "valence": 0.5,
            "energy": energy,
            "intensity": 0.5,
            "note": note,
            "tags": ["work"],
            "context": {"entry_method": "test"},
        }
    )


# ---------------------------------------------------------------------------
# Preferences: round-trip and in-place migration
# ---------------------------------------------------------------------------


def test_preferences_round_trip_includes_delivery_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    store = LotusCheckinStore("alice")

    saved = store.save_preferences(
        {
            "timezone": "Europe/Warsaw",
            "reminder_enabled": True,
            "reminder_times": ["08:30", "20:00"],
            "reminder_weekdays": [0, 2, 4],
            "quiet_start": "22:00",
            "quiet_end": "07:00",
            "snooze_minutes": 45,
            "channel": "ntfy",
            "min_hours_between": 6,
            "skip_if_checked_in": False,
            "message_style": "spark",
            "paused_until": "2026-08-14T09:00:00+00:00",
            "insights_enabled": True,
            "insights_frequency": "biweekly",
            "insights_weekday": 0,
            "insights_time": "18:15",
        }
    )

    assert saved == LotusCheckinStore("alice").get_preferences()
    assert saved["channel"] == "ntfy"
    assert saved["min_hours_between"] == 6
    assert saved["skip_if_checked_in"] is False
    assert saved["message_style"] == "spark"
    assert saved["insights_frequency"] == "biweekly"
    assert saved["reminder_times"] == ["08:30", "20:00"]

    # A partial write must not blank the untouched fields.
    partial = LotusCheckinStore("alice").save_preferences({"paused_until": None})
    assert partial["paused_until"] is None
    assert partial["reminder_times"] == ["08:30", "20:00"]
    assert partial["channel"] == "ntfy"


def test_old_schema_database_upgrades_in_place(monkeypatch, tmp_path):
    """An install written before this milestone keeps its data and gains defaults."""
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    directory = tmp_path / "lotus" / "users" / owner_storage_key("alice")
    directory.mkdir(parents=True)
    conn = sqlite3.connect(directory / "mood.db")
    conn.executescript(
        """
        CREATE TABLE lotus_preferences (
            id                 INTEGER PRIMARY KEY CHECK (id = 1),
            timezone           TEXT NOT NULL DEFAULT 'UTC',
            reminder_enabled   INTEGER NOT NULL DEFAULT 0,
            reminder_times     TEXT NOT NULL DEFAULT '[]',
            reminder_weekdays  TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
            quiet_start        TEXT,
            quiet_end          TEXT,
            snooze_minutes     INTEGER NOT NULL DEFAULT 30,
            updated_at         TEXT NOT NULL
        );
        INSERT INTO lotus_preferences VALUES
            (1, 'America/New_York', 1, '["21:00"]', '[0,1,2]', '23:00', '06:00', 15, '2026-01-01T00:00:00');
        """
    )
    conn.commit()
    conn.close()

    prefs = LotusCheckinStore("alice").get_preferences()

    assert prefs["timezone"] == "America/New_York"
    assert prefs["reminder_times"] == ["21:00"]
    assert prefs["reminder_weekdays"] == [0, 1, 2]
    assert prefs["quiet_start"] == "23:00"
    assert prefs["snooze_minutes"] == 15
    # New columns arrive with their defaults rather than exploding.
    assert prefs["channel"] == "inherit"
    assert prefs["message_style"] == "plain"
    assert prefs["insights_enabled"] is False
    assert prefs["paused_until"] is None
    assert prefs["access_local"] is True
    assert prefs["access_lan"] is True
    assert prefs["access_api"] is False

    # And the upgraded row still saves.
    updated = LotusCheckinStore("alice").save_preferences({"channel": "email"})
    assert updated["channel"] == "email"
    assert updated["reminder_times"] == ["21:00"]


def test_access_policy_is_owner_scoped_and_defaults_are_private(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from src.lotus_access import (
        get_lotus_access_policy,
        lotus_endpoint_allowed,
        save_lotus_access_policy,
    )

    assert get_lotus_access_policy("alice") == {
        "local": True,
        "lan": True,
        "api": False,
    }
    assert lotus_endpoint_allowed("alice", "http://localhost:11434/v1") is True
    assert lotus_endpoint_allowed("alice", "http://192.168.1.50:8000/v1") is True
    assert lotus_endpoint_allowed("alice", "https://api.openai.com/v1") is False

    saved = save_lotus_access_policy(
        "alice", {"local": False, "lan": False, "api": True}
    )
    assert saved == {"local": False, "lan": False, "api": True}
    assert lotus_endpoint_allowed("alice", "https://api.openai.com/v1") is True
    assert lotus_endpoint_allowed("alice", "http://localhost:11434/v1") is False
    # Bob retains defaults in a physically separate database/policy.
    assert get_lotus_access_policy("bob") == {
        "local": True,
        "lan": True,
        "api": False,
    }


# ---------------------------------------------------------------------------
# The pure scheduling function
# ---------------------------------------------------------------------------


def test_notification_is_due_inside_the_configured_window():
    prefs = _base_prefs(reminder_times=["20:00"])
    due = due_notifications(prefs, datetime(2026, 8, 10, 20, 0, 30, tzinfo=UTC))
    assert [n.kind for n in due] == ["checkin"]
    assert due[0].dedupe_key == "lotus-checkin-ownerhash-20:00"
    # Two minutes later the window has closed.
    assert due_notifications(prefs, datetime(2026, 8, 10, 20, 2, tzinfo=UTC)) == []


def test_disabled_and_multiple_times():
    prefs = _base_prefs(reminder_times=["08:30", "20:00"])
    assert [n.slot for n in due_notifications(prefs, datetime(2026, 8, 10, 8, 30, tzinfo=UTC))] == ["08:30"]
    assert due_notifications(_base_prefs(reminder_enabled=False), datetime(2026, 8, 10, 20, 0, tzinfo=UTC)) == []


def test_weekday_selection_suppresses_the_notification():
    # 2026-08-10 is a Monday (weekday 0); 2026-08-11 a Tuesday.
    prefs = _base_prefs(reminder_weekdays=[0])
    assert due_notifications(prefs, datetime(2026, 8, 10, 20, 0, tzinfo=UTC))
    assert due_notifications(prefs, datetime(2026, 8, 11, 20, 0, tzinfo=UTC)) == []


def test_quiet_hours_suppress_including_a_range_crossing_midnight():
    quiet = _base_prefs(reminder_times=["23:30"], quiet_start="22:00", quiet_end="07:00")
    assert due_notifications(quiet, datetime(2026, 8, 10, 23, 30, tzinfo=UTC)) == []
    # 06:30 is still inside the 22:00 -> 07:00 window.
    early = _base_prefs(reminder_times=["06:30"], quiet_start="22:00", quiet_end="07:00")
    assert due_notifications(early, datetime(2026, 8, 10, 6, 30, tzinfo=UTC)) == []
    # 08:00 is outside it.
    after = _base_prefs(reminder_times=["08:00"], quiet_start="22:00", quiet_end="07:00")
    assert due_notifications(after, datetime(2026, 8, 10, 8, 0, tzinfo=UTC))
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 0).time(), *_clock("22:00", "07:00"))
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0).time(), *_clock("22:00", "07:00"))


def _clock(start, end):
    from src.lotus_notifications import _parse_clock

    return _parse_clock(start), _parse_clock(end)


def test_min_hours_between_suppresses_a_second_notification():
    prefs = _base_prefs(reminder_times=["20:00"], min_hours_between=12)
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    assert due_notifications(prefs, now, None, now - timedelta(hours=2)) == []
    assert due_notifications(prefs, now, None, now - timedelta(hours=13))


def test_skip_if_checked_in_suppresses_only_after_a_checkin_that_day():
    prefs = _base_prefs(reminder_times=["20:00"], skip_if_checked_in=True)
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    assert due_notifications(prefs, now, datetime(2026, 8, 10, 9, 0, tzinfo=UTC)) == []
    assert due_notifications(prefs, now, datetime(2026, 8, 9, 21, 0, tzinfo=UTC))


def test_paused_until_suppresses_everything():
    prefs = _base_prefs(reminder_times=["20:00"], paused_until="2026-08-10T23:00:00+00:00")
    assert due_notifications(prefs, datetime(2026, 8, 10, 20, 0, tzinfo=UTC)) == []
    prefs["paused_until"] = "2026-08-10T19:00:00+00:00"
    assert due_notifications(prefs, datetime(2026, 8, 10, 20, 0, tzinfo=UTC))


def test_local_time_follows_dst_for_a_real_iana_zone():
    prefs = _base_prefs(timezone="America/New_York", reminder_times=["09:00"])
    # August: EDT (UTC-4), so 09:00 local is 13:00 UTC.
    assert due_notifications(prefs, datetime(2026, 8, 10, 13, 0, tzinfo=UTC))
    assert due_notifications(prefs, datetime(2026, 8, 10, 14, 0, tzinfo=UTC)) == []
    # January: EST (UTC-5), so the same wall-clock time is 14:00 UTC.
    assert due_notifications(prefs, datetime(2026, 1, 12, 14, 0, tzinfo=UTC))
    assert due_notifications(prefs, datetime(2026, 1, 12, 13, 0, tzinfo=UTC)) == []


def test_unknown_timezone_falls_back_to_utc_instead_of_raising():
    prefs = _base_prefs(timezone="Not/AZone", reminder_times=["20:00"])
    assert due_notifications(prefs, datetime(2026, 8, 10, 20, 0, tzinfo=UTC))


def test_insight_cadence_respects_frequency_and_weekday():
    prefs = _base_prefs(
        reminder_enabled=False,
        insights_enabled=True,
        insights_frequency="weekly",
        insights_weekday=6,  # Sunday
        insights_time="09:00",
    )
    sunday = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    assert [n.kind for n in due_notifications(prefs, sunday)] == ["insight"]
    # Wrong weekday.
    assert due_notifications(prefs, datetime(2026, 8, 10, 9, 0, tzinfo=UTC)) == []
    # Sent three days ago — weekly cadence not yet satisfied.
    assert due_notifications(prefs, sunday, last_insight_at=sunday - timedelta(days=3)) == []
    assert due_notifications(prefs, sunday, last_insight_at=sunday - timedelta(days=8))
    prefs["insights_frequency"] = "monthly"
    assert due_notifications(prefs, sunday, last_insight_at=sunday - timedelta(days=8)) == []


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_dispatch_passes_owner_and_channel_override_and_records_the_row(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from routes import note_routes
    from src import lotus_notifications

    store = LotusCheckinStore("alice")
    store.save_preferences(
        {
            "timezone": "UTC",
            "reminder_enabled": True,
            "reminder_times": ["20:00"],
            "channel": "ntfy",
            "skip_if_checked_in": False,
        }
    )

    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"channel": "ntfy", "ntfy_sent": True, "browser_sent": True, "synthesis": None}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)

    sent = asyncio.run(
        lotus_notifications.run_owner_tick("alice", now_utc=datetime(2026, 8, 10, 20, 0, tzinfo=UTC))
    )

    assert [n.kind for n in sent] == ["checkin"]
    assert len(calls) == 1
    assert calls[0]["owner"] == "alice"
    assert calls[0]["settings_override"]["reminder_channel"] == "ntfy"
    # Global LLM synthesis is explicitly turned off for the plain style so
    # mood data cannot be shipped to whatever the utility endpoint is.
    assert calls[0]["settings_override"]["reminder_llm_synthesis"] is False
    assert calls[0]["persist_dedupe"] is False
    assert calls[0]["note_id"] == f"lotus-checkin-{owner_storage_key('alice')}-20:00"

    history = store.list_notifications()
    assert len(history) == 1
    assert history[0]["kind"] == "checkin"
    assert history[0]["channel"] == "ntfy"
    assert history[0]["delivered"] is True
    assert store.last_notification_at() is not None


def test_delivered_occurrence_is_not_repeated_on_overlapping_ticks(monkeypatch, tmp_path):
    """The 90-second due window overlaps two 60-second scheduler ticks."""
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from routes import note_routes
    from src import lotus_notifications

    store = LotusCheckinStore("alice")
    store.save_preferences(
        {
            "timezone": "UTC",
            "reminder_enabled": True,
            "reminder_times": ["20:00"],
            "channel": "browser",
            "skip_if_checked_in": False,
        }
    )
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"browser_sent": True, "synthesis": None}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    asyncio.run(
        lotus_notifications.run_owner_tick(
            "alice", now_utc=datetime(2026, 8, 10, 20, 0, 5, tzinfo=UTC)
        )
    )
    second = asyncio.run(
        lotus_notifications.run_owner_tick(
            "alice", now_utc=datetime(2026, 8, 10, 20, 1, 5, tzinfo=UTC)
        )
    )

    assert second == []
    assert len(calls) == 1
    assert len(store.list_notifications()) == 1


def test_lotus_can_bypass_username_named_notes_cache(monkeypatch, tmp_path):
    from routes import note_routes

    monkeypatch.setattr(note_routes, "DATA_DIR", str(tmp_path))
    result = asyncio.run(
        note_routes.dispatch_reminder(
            title="Lotus test",
            note_body="Check in",
            note_id="lotus-ownerhash-20:00",
            owner="alice@example.com",
            queue_browser=False,
            settings_override={
                "reminder_channel": "browser",
                "reminder_llm_synthesis": False,
            },
            persist_dedupe=False,
        )
    )

    assert result["browser_sent"] is True
    assert list(tmp_path.glob("note_pings_*.json")) == []


def test_llm_phrasing_is_dropped_when_the_utility_endpoint_is_not_local(monkeypatch):
    from src import lotus_notifications

    monkeypatch.setattr(lotus_notifications, "llm_phrasing_allowed", lambda owner: False)
    override = settings_override_for(_base_prefs(channel="email", message_style="spark"), "alice")
    assert override["reminder_llm_synthesis"] is False
    assert "reminder_llm_persona" not in override

    monkeypatch.setattr(lotus_notifications, "llm_phrasing_allowed", lambda owner: True)
    override = settings_override_for(_base_prefs(channel="email", message_style="spark"), "alice")
    assert override["reminder_llm_synthesis"] is True
    assert override["reminder_llm_persona"] == "spark"


def test_explicit_channel_wins_over_the_global_setting():
    assert resolved_channel(_base_prefs(channel="webhook")) == "webhook"
    assert resolved_channel(_base_prefs(channel="nonsense")) in {"browser", "email", "ntfy", "webhook"}


def test_scanner_skips_owners_without_a_lotus_database(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from src.builtin_actions import TaskNoop, action_lotus_reminders

    assert owner_has_lotus_data("nobody") is False
    with pytest.raises(TaskNoop):
        asyncio.run(action_lotus_reminders(owner="nobody"))
    # Probing must not have created a database for that account.
    assert owner_has_lotus_data("nobody") is False


def test_test_notification_goes_through_the_real_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from routes import note_routes
    from src.lotus_notifications import send_test_notification

    LotusCheckinStore("alice").save_preferences({"channel": "browser"})
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"browser_sent": True, "synthesis": None}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    result = asyncio.run(send_test_notification("alice"))

    assert result["delivered"] is True
    assert result["channel"] == "browser"
    assert calls[0]["owner"] == "alice"
    assert LotusCheckinStore("alice").list_notifications()[0]["kind"] == "test"


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def test_insights_are_deterministic_and_carry_sample_sizes(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    store = LotusCheckinStore("alice")
    base = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    for day in range(6):
        _checkin(store, base=base + timedelta(days=day), emotion="calm", energy=0.8)
        _checkin(store, base=base + timedelta(days=day, hours=12), emotion="tired", energy=0.2)

    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    payload = compute_insights(store, days=30, now=now)

    assert payload["sample_size"] == 12
    assert payload["averages"]["energy"]["sample_size"] == 12
    assert payload["averages"]["energy"]["value"] == 0.5
    assert payload["top_emotions"][0]["count"] == 6
    assert {item["label"] for item in payload["top_emotions"]} == {"calm", "tired"}
    assert payload["notes_accessed"] is False
    assert "clinical" in payload["disclaimer"]

    morning = payload["time_of_day"]["highest_energy"]
    evening = payload["time_of_day"]["lowest_energy"]
    assert morning["bucket"] == "early morning" and morning["entries"] == 6
    assert evening["bucket"] == "evening" and evening["entries"] == 6

    # Deterministic: identical inputs, identical output.
    assert compute_insights(store, days=30, now=now) == payload


def test_no_note_text_appears_in_any_insight_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    store = LotusCheckinStore("alice")
    for hour in range(5):
        _checkin(store, offset_hours=hour * 6)

    payload = compute_insights(store, days=30, now=datetime(2026, 8, 6, tzinfo=UTC))
    serialized = json.dumps(payload, default=str)
    assert NOTE_TEXT not in serialized
    assert "synthetic note" not in serialized
    assert NOTE_TEXT not in render_observations(payload)


def test_insights_over_an_empty_store_say_so_instead_of_inventing(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    payload = compute_insights(LotusCheckinStore("alice"), days=30)
    assert payload["sample_size"] == 0
    assert payload["averages"]["energy"]["value"] is None
    assert "No check-ins logged" in render_observations(payload)


# ---------------------------------------------------------------------------
# Model access
# ---------------------------------------------------------------------------


def test_wellbeing_tool_returns_aggregates_only(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from src.tools.wellbeing import do_manage_wellbeing

    store = LotusCheckinStore("alice")
    for day in range(4):
        _checkin(store, base=datetime(2026, 8, 3, 9, 0, tzinfo=UTC) + timedelta(days=day))

    summary = asyncio.run(do_manage_wellbeing(json.dumps({"action": "summary", "days": 30}), owner="alice"))
    assert summary["exit_code"] == 0
    assert summary["sample_size"] == 4
    assert summary["averages"]["energy"]["sample_size"] == 4
    assert "sample size" in summary["reporting_rules"]
    assert NOTE_TEXT not in json.dumps(summary, default=str)

    patterns = asyncio.run(do_manage_wellbeing(json.dumps({"action": "patterns"}), owner="alice"))
    assert patterns["minimum_bucket_entries"] >= 1
    assert "time_of_day" in patterns and "weekday" in patterns
    assert NOTE_TEXT not in json.dumps(patterns, default=str)

    latest = asyncio.run(do_manage_wellbeing(json.dumps({"action": "latest"}), owner="alice"))
    assert latest["latest"]["emotion_label"] == "calm"
    assert "note" not in latest["latest"]
    assert NOTE_TEXT not in json.dumps(latest, default=str)


def test_wellbeing_tool_logs_a_model_entered_checkin(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from src.tools.wellbeing import do_manage_wellbeing

    result = asyncio.run(
        do_manage_wellbeing(
            json.dumps(
                {
                    "action": "log_checkin",
                    "emotion_label": "drained",
                    "emotion_family": "unpleasant_low",
                    "tags": ["deadline"],
                }
            ),
            owner="alice",
        )
    )
    assert result["exit_code"] == 0
    stored = LotusCheckinStore("alice").list_checkins()
    assert len(stored) == 1
    # UI-entered and model-entered rows stay distinguishable.
    assert stored[0]["context"]["entry_method"] == "model_tool"

    bad = asyncio.run(
        do_manage_wellbeing(json.dumps({"action": "log_checkin", "emotion_label": "x"}), owner="alice")
    )
    assert bad["exit_code"] == 1


def test_wellbeing_tool_is_refused_on_a_non_local_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from src import tool_execution, tool_implementations

    _checkin(LotusCheckinStore("alice"))
    block = SimpleNamespace(tool_type="manage_wellbeing", content=json.dumps({"action": "summary"}))

    # No resolvable session -> cannot prove the endpoint is local -> refuse.
    _desc, refused = asyncio.run(tool_execution.execute_tool_block(block, owner="alice"))
    assert refused["exit_code"] == 1
    assert "policy" in refused["error"]
    assert "sample_size" not in refused

    monkeypatch.setattr(tool_implementations, "is_local_session", lambda *args, **kwargs: False)
    _desc, remote = asyncio.run(
        tool_execution.execute_tool_block(block, owner="alice", session_id="s1")
    )
    assert remote["exit_code"] == 1

    monkeypatch.setattr(tool_implementations, "is_local_session", lambda *args, **kwargs: True)
    _desc, allowed = asyncio.run(
        tool_execution.execute_tool_block(block, owner="alice", session_id="s1")
    )
    assert allowed["exit_code"] == 0
    assert allowed["sample_size"] == 1


def test_wellbeing_session_access_policy_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    from src import ai_interaction
    from src.tools.wellbeing import is_local_session

    assert is_local_session(None) is False
    monkeypatch.setattr(ai_interaction, "_session_manager", None, raising=False)
    assert is_local_session("s1") is False

    monkeypatch.setattr(
        ai_interaction,
        "_session_manager",
        SimpleNamespace(
            get_session=lambda sid: SimpleNamespace(endpoint_url="http://127.0.0.1:11434/v1")
        ),
        raising=False,
    )
    assert is_local_session("s1", owner="alice") is True

    monkeypatch.setattr(
        ai_interaction,
        "_session_manager",
        SimpleNamespace(
            get_session=lambda sid: SimpleNamespace(endpoint_url="https://api.openai.com/v1")
        ),
        raising=False,
    )
    assert is_local_session("s1", owner="alice") is False

    LotusCheckinStore("alice").save_preferences({"access_api": True})
    assert is_local_session("s1", owner="alice") is True


def test_wellbeing_tool_is_wired_into_the_agent_surfaces():
    from src.agent_loop import (
        _DOMAIN_RULES,
        _DOMAIN_TOOL_MAP,
        _STARVED_DOMAIN_LABELS,
        TOOL_SECTIONS,
        _classify_agent_request,
    )
    from src.agent_tools import TOOL_TAGS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    assert "manage_wellbeing" in TOOL_TAGS
    assert "manage_wellbeing" in TOOL_SECTIONS
    assert "manage_wellbeing" in BUILTIN_TOOL_DESCRIPTIONS
    assert _DOMAIN_TOOL_MAP["wellbeing"] == {"manage_wellbeing"}
    assert "wellbeing" in _DOMAIN_RULES and "wellbeing" in _STARVED_DOMAIN_LABELS
    assert any(
        s.get("function", {}).get("name") == "manage_wellbeing" for s in FUNCTION_TOOL_SCHEMAS
    )

    for phrase in ("how has my mood been lately", "plan my week", "am I burning out"):
        assert "wellbeing" in _classify_agent_request([], phrase)["domains"], phrase


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch, owner="alice"):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    app = FastAPI()
    app.include_router(setup_lotus_routes())
    app.dependency_overrides[require_user] = lambda: owner
    return TestClient(app)


def test_routes_expose_snooze_notifications_and_insights(monkeypatch, tmp_path):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/api/lotus/access-policy").json() == {
        "local": True,
        "lan": True,
        "api": False,
    }
    changed = client.put(
        "/api/lotus/access-policy",
        json={"local": True, "lan": False, "api": True},
    )
    assert changed.status_code == 200
    assert changed.json() == {"local": True, "lan": False, "api": True}

    snoozed = client.post("/api/lotus/snooze", json={"minutes": 60})
    assert snoozed.status_code == 200
    paused_until = snoozed.json()["paused_until"]
    assert paused_until and client.get("/api/lotus/preferences").json()["paused_until"] == paused_until
    assert client.request("DELETE", "/api/lotus/snooze").json()["paused_until"] is None

    assert client.get("/api/lotus/notifications?limit=5").json() == {"notifications": []}

    insights = client.get("/api/lotus/insights?days=14")
    assert insights.status_code == 200
    assert insights.json()["sample_size"] == 0
    assert "summary" in insights.json()


def test_routes_reject_invalid_delivery_preferences(monkeypatch, tmp_path):
    client = _client(tmp_path, monkeypatch)
    valid = {"timezone": "UTC", "reminder_times": ["09:00"]}

    assert client.put("/api/lotus/preferences", json={**valid, "channel": "carrier-pigeon"}).status_code == 422
    assert client.put("/api/lotus/preferences", json={**valid, "message_style": "not-a-persona"}).status_code == 422
    assert client.put("/api/lotus/preferences", json={**valid, "insights_time": "9am"}).status_code == 422
    assert client.put("/api/lotus/preferences", json={**valid, "insights_weekday": 9}).status_code == 422
    assert client.put("/api/lotus/preferences", json={**valid, "min_hours_between": -1}).status_code == 422
    assert client.put("/api/lotus/preferences", json={**valid, "paused_until": "2026-08-10T09:00:00"}).status_code == 422
    assert client.put("/api/lotus/preferences", json={**valid, "reminder_times": ["01:00"] * 9}).status_code == 422

    ok = client.put(
        "/api/lotus/preferences",
        json={**valid, "channel": "email", "message_style": "razor", "insights_weekday": 3},
    )
    assert ok.status_code == 200
    assert ok.json()["channel"] == "email"


def test_test_notification_endpoint_uses_the_real_dispatcher(monkeypatch, tmp_path):
    from routes import note_routes

    client = _client(tmp_path, monkeypatch)
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"browser_sent": True, "synthesis": None}

    monkeypatch.setattr(note_routes, "dispatch_reminder", fake_dispatch)
    response = client.post("/api/lotus/preferences/test")

    assert response.status_code == 200
    assert response.json()["delivered"] is True
    assert calls[0]["owner"] == "alice"
    assert len(client.get("/api/lotus/notifications").json()["notifications"]) == 1


# ---------------------------------------------------------------------------
# Frontend wiring
# ---------------------------------------------------------------------------


def test_lotus_reminders_tab_is_wired_to_the_delivery_endpoints():
    root = Path(__file__).resolve().parent.parent
    lotus_js = (root / "static" / "js" / "lotus.js").read_text(encoding="utf-8")
    style = (root / "static" / "style.css").read_text(encoding="utf-8")

    for endpoint in ("/preferences/test", "/snooze", "/notifications?limit="):
        assert endpoint in lotus_js, endpoint
    for control in (
        "lotus-reminder-enabled",
        "lotus-times",
        "lotus-add-time",
        "lotus-weekdays",
        "lotus-quiet-start",
        "lotus-quiet-end",
        "lotus-min-hours",
        "lotus-skip-checked-in",
        "lotus-channel",
        "lotus-message-style",
        "lotus-insights-enabled",
        "lotus-insights-frequency",
        "lotus-insights-weekday",
        "lotus-insights-time",
        "lotus-test-notification",
        "lotus-snooze",
        "lotus-notification-list",
    ):
        assert control in lotus_js, control

    # The stub copy is gone from both the status message and the tab hint.
    assert "later milestone" not in lotus_js
    assert "MAX_REMINDER_TIMES = 8" in lotus_js
    assert ".lotus-weekdays" in style
    assert ".lotus-notification-list" in style


def test_settings_privacy_menu_controls_lotus_endpoint_scopes():
    root = Path(__file__).resolve().parent.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    settings_js = (root / "static" / "js" / "settings.js").read_text(encoding="utf-8")

    assert 'data-settings-tab="privacy"' in html
    assert 'data-settings-panel="privacy"' in html
    for control in (
        "set-lotus-access-local",
        "set-lotus-access-lan",
        "set-lotus-access-api",
        "set-lotus-access-save",
    ):
        assert control in html
        assert control in settings_js
    assert "/api/lotus/access-policy" in settings_js
    assert "third-party provider" in html


def test_scheduler_runs_and_cancels_the_lotus_loop():
    root = Path(__file__).resolve().parent.parent
    scheduler = (root / "src" / "task_scheduler.py").read_text(encoding="utf-8")

    assert "self._lotus_pings_task = asyncio.create_task(self._lotus_pings_loop())" in scheduler
    assert '"_lotus_pings_task"' in scheduler
    assert "async def _lotus_pings_loop" in scheduler


def test_lotus_reminder_action_is_not_a_user_schedulable_task():
    from src.builtin_actions import BUILTIN_ACTIONS

    assert "lotus_reminders" not in BUILTIN_ACTIONS
