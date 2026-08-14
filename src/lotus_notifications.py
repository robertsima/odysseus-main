"""Lotus check-in reminders and insight notifications.

The scheduling rules live in :func:`due_notifications`, which is pure: it takes
a preferences dict, an instant, and two timestamps, and returns what should be
sent. No database, no clock, no settings — so every rule (weekday selection,
quiet hours across midnight, minimum gap, snooze, DST) is unit-testable without
patching anything.

Delivery reuses the existing reminder stack (``dispatch_reminder``) rather than
building a second notification path; the only Lotus-specific parts are the
per-owner channel override and the fact that delivery is recorded inside the
owner's own Lotus database instead of a file named after them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.lotus_checkins import LotusCheckinStore, owner_storage_key

logger = logging.getLogger(__name__)

#: The scheduler ticks every 60s; a 90s window guarantees each configured time
#: lands inside at least one tick without needing a second-accurate wake-up.
DEFAULT_WINDOW_SECONDS = 90

#: "no LLM phrasing" — the deterministic body is sent exactly as composed.
PLAIN_MESSAGE_STYLE = "plain"

CHANNEL_CHOICES = ("inherit", "browser", "email", "ntfy", "webhook")
INSIGHT_FREQUENCIES = {"weekly": 6, "biweekly": 13, "monthly": 27}

_CHECKIN_TITLE = "Lotus check-in"
_INSIGHT_TITLE = "Lotus observations"


@dataclass(frozen=True)
class Notification:
    """One thing that is due right now for one owner."""

    kind: str  # "checkin" | "insight"
    slot: str  # "HH:MM" for a check-in nudge, the local date for an insight
    scheduled_local: datetime
    dedupe_key: str


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Resolve a stored IANA name, falling back to UTC.

    Stored as a name rather than a fixed offset so reminder times keep meaning
    the same wall-clock hour across a DST transition.
    """
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            text = str(value)
            moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _parse_clock(value: Any) -> time | None:
    text = str(value or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hour, minute = int(text[:2]), int(text[3:])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def in_quiet_hours(moment: time, start: time | None, end: time | None) -> bool:
    """Quiet-hours test that handles a window running through midnight."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def _local_instant(day: date, clock: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, clock).replace(tzinfo=zone)


def due_notifications(
    prefs: dict[str, Any],
    now_utc: datetime,
    last_checkin_at: Any = None,
    last_sent_at: Any = None,
    *,
    last_insight_at: Any = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[Notification]:
    """Return the notifications whose moment falls in the current tick.

    A slot fires when ``now`` is inside ``[scheduled, scheduled + window)`` —
    never early, and at most one tick late.
    """
    now = _as_datetime(now_utc) or datetime.now(UTC)
    reminders_on = bool(prefs.get("reminder_enabled"))
    insights_on = bool(prefs.get("insights_enabled"))
    if not reminders_on and not insights_on:
        return []

    paused_until = _as_datetime(prefs.get("paused_until"))
    if paused_until and now < paused_until:
        return []

    zone = resolve_timezone(prefs.get("timezone"))
    local_now = now.astimezone(zone)
    window = timedelta(seconds=max(1, int(window_seconds)))
    quiet_start = _parse_clock(prefs.get("quiet_start"))
    quiet_end = _parse_clock(prefs.get("quiet_end"))

    last_sent = _as_datetime(last_sent_at)
    min_gap = timedelta(hours=max(0, int(prefs.get("min_hours_between") or 0)))
    if last_sent and min_gap and now - last_sent < min_gap:
        return []

    last_checkin = _as_datetime(last_checkin_at)
    skip_if_checked_in = bool(prefs.get("skip_if_checked_in"))
    owner_key = str(prefs.get("_owner_key") or "")

    due: list[Notification] = []

    if reminders_on:
        weekdays = {int(day) for day in (prefs.get("reminder_weekdays") or []) if 0 <= int(day) <= 6}
        for raw_time in prefs.get("reminder_times") or []:
            clock = _parse_clock(raw_time)
            if clock is None:
                continue
            # Check yesterday's slot too: a late tick just after local midnight
            # still belongs to the previous local day's schedule.
            for day in (local_now.date(), local_now.date() - timedelta(days=1)):
                scheduled = _local_instant(day, clock, zone)
                if not (scheduled <= local_now < scheduled + window):
                    continue
                if scheduled.weekday() not in weekdays:
                    continue
                if in_quiet_hours(clock, quiet_start, quiet_end):
                    continue
                if skip_if_checked_in and last_checkin is not None:
                    day_start = _local_instant(day, time(0, 0), zone).astimezone(UTC)
                    if last_checkin >= day_start:
                        continue
                due.append(
                    Notification(
                        kind="checkin",
                        slot=f"{clock.hour:02d}:{clock.minute:02d}",
                        scheduled_local=scheduled,
                        dedupe_key=f"lotus-checkin-{owner_key}-{clock.hour:02d}:{clock.minute:02d}",
                    )
                )
                break

    if insights_on:
        clock = _parse_clock(prefs.get("insights_time")) or time(9, 0)
        weekday = int(prefs.get("insights_weekday") or 0)
        min_days = INSIGHT_FREQUENCIES.get(str(prefs.get("insights_frequency") or "weekly"), 6)
        last_insight = _as_datetime(last_insight_at)
        for day in (local_now.date(), local_now.date() - timedelta(days=1)):
            scheduled = _local_instant(day, clock, zone)
            if not (scheduled <= local_now < scheduled + window):
                continue
            if scheduled.weekday() != weekday:
                continue
            if in_quiet_hours(clock, quiet_start, quiet_end):
                continue
            if last_insight and (now - last_insight) < timedelta(days=min_days):
                continue
            due.append(
                Notification(
                    kind="insight",
                    slot=day.isoformat(),
                    scheduled_local=scheduled,
                    dedupe_key=f"lotus-insight-{owner_key}-{day.isoformat()}",
                )
            )
            break

    return due


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def resolved_channel(prefs: dict[str, Any]) -> str:
    """The channel this owner's Lotus notifications go out on.

    ``inherit`` means "whatever the global reminder channel is", so a user who
    changes the app-wide channel does not have to update Lotus separately.
    """
    channel = str(prefs.get("channel") or "inherit").strip().lower()
    if channel not in CHANNEL_CHOICES:
        channel = "inherit"
    if channel != "inherit":
        return channel
    try:
        from src.settings import load_settings

        return str(load_settings().get("reminder_channel") or "browser")
    except Exception:
        return "browser"


def llm_phrasing_allowed(owner: str) -> bool:
    """Whether the utility model may rewrite a Lotus notification body.

    The resolved utility endpoint must pass the same owner-controlled
    local/LAN/API policy the agent loop uses for Lotus tools. Private check-in
    note text is never included in the body sent for phrasing.
    """
    try:
        from src.endpoint_resolver import resolve_endpoint
        from src.lotus_access import lotus_endpoint_allowed

        url, model, _headers = resolve_endpoint("utility", owner=owner or None)
        if not url:
            url, model, _headers = resolve_endpoint("default", owner=owner or None)
        return bool(url and model and lotus_endpoint_allowed(owner, url))
    except Exception as error:
        logger.debug("Lotus: could not classify the utility endpoint: %s", error)
        return False


def settings_override_for(prefs: dict[str, Any], owner: str) -> dict[str, Any]:
    """Per-call settings handed to ``dispatch_reminder``."""
    override: dict[str, Any] = {"reminder_channel": resolved_channel(prefs)}
    style = str(prefs.get("message_style") or PLAIN_MESSAGE_STYLE).strip().lower()
    if style and style != PLAIN_MESSAGE_STYLE and llm_phrasing_allowed(owner):
        override["reminder_llm_synthesis"] = True
        override["reminder_llm_persona"] = style
    else:
        # Explicit: without this a global `reminder_llm_synthesis=true` would
        # ship mood text to whatever the utility endpoint happens to be.
        override["reminder_llm_synthesis"] = False
    return override


def compose_body(notification: Notification, store: LotusCheckinStore, *, days: int = 30) -> tuple[str, str]:
    """(title, body) for one due notification. Never includes note text."""
    if notification.kind == "insight":
        from src.lotus_insights import compute_insights, render_observations

        payload = compute_insights(store, days=days)
        return _INSIGHT_TITLE, render_observations(payload)
    return (
        _CHECKIN_TITLE,
        "A moment for yourself — how are you feeling right now? "
        "Open Lotus to log a check-in.",
    )


def _delivered(result: dict[str, Any], channel: str) -> bool:
    if channel == "email":
        return bool(result.get("email_sent"))
    if channel == "ntfy":
        return bool(result.get("ntfy_sent"))
    if channel == "webhook":
        return bool(result.get("webhook_sent"))
    return bool(result.get("browser_sent"))


async def deliver(
    owner: str,
    store: LotusCheckinStore,
    notification: Notification,
    prefs: dict[str, Any],
) -> dict[str, Any]:
    """Send one notification and record the attempt in the owner's database."""
    from routes.note_routes import dispatch_reminder

    title, body = compose_body(notification, store)
    override = settings_override_for(prefs, owner)
    channel = str(override["reminder_channel"])
    result = await dispatch_reminder(
        title=title,
        note_body=body,
        note_id=notification.dedupe_key,
        owner=owner or "",
        settings_override=override,
        # Lotus delivery state belongs in the hashed owner database. The
        # generic Notes cache uses an owner-derived filename on disk.
        persist_dedupe=False,
    )
    delivered = _delivered(result, channel) and not result.get("skipped")
    store.record_notification(
        kind=notification.kind,
        title=title,
        body=(result.get("synthesis") or body),
        channel=channel,
        dedupe_key=notification.dedupe_key,
        delivered=delivered,
    )
    return result


async def run_owner_tick(
    owner: str,
    *,
    now_utc: datetime | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[Notification]:
    """Evaluate and deliver whatever is due for one owner. Returns what was sent."""
    store = LotusCheckinStore(owner)
    prefs = {**store.get_preferences(), "_owner_key": owner_storage_key(owner)}
    pending = due_notifications(
        prefs,
        now_utc or datetime.now(UTC),
        store.last_checkin_at(),
        store.last_notification_at(kind="checkin"),
        last_insight_at=store.last_notification_at(kind="insight"),
        window_seconds=window_seconds,
    )
    sent: list[Notification] = []
    for notification in pending:
        occurrence_utc = notification.scheduled_local.astimezone(UTC).isoformat()
        if store.notification_delivered_since(notification.dedupe_key, occurrence_utc):
            continue
        try:
            await deliver(owner, store, notification, prefs)
            sent.append(notification)
        except Exception as error:
            logger.warning("Lotus notification dispatch failed (%s): %s", notification.kind, error)
    return sent


async def send_test_notification(owner: str) -> dict[str, Any]:
    """Deliver a check-in nudge immediately, through the real path.

    Uses a unique dedupe key so the 25-minute suppression in
    ``dispatch_reminder`` cannot swallow the user's own verification.
    """
    store = LotusCheckinStore(owner)
    prefs = store.get_preferences()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    notification = Notification(
        kind="test",
        slot="test",
        scheduled_local=datetime.now(UTC),
        dedupe_key=f"lotus-test-{owner_storage_key(owner)}-{stamp}",
    )
    override = settings_override_for(prefs, owner)
    channel = str(override["reminder_channel"])
    from routes.note_routes import dispatch_reminder

    title = "Lotus test notification"
    body = "This is how a Lotus reminder will reach you."
    result = await dispatch_reminder(
        title=title,
        note_body=body,
        note_id=notification.dedupe_key,
        owner=owner or "",
        settings_override=override,
        persist_dedupe=False,
    )
    delivered = _delivered(result, channel)
    store.record_notification(
        kind="test",
        title=title,
        body=(result.get("synthesis") or body),
        channel=channel,
        dedupe_key=notification.dedupe_key,
        delivered=delivered,
    )
    return {"channel": channel, "delivered": delivered, "detail": result}


def known_owners() -> Iterable[str]:
    """Every owner the reminder scanner should visit.

    Lotus directories are hashes, so owners cannot be recovered from disk —
    the real account list has to be enumerated and hashed forward. The empty
    owner covers auth-disabled / single-user installs.
    """
    owners: list[str] = []
    try:
        from core.auth import AuthManager

        owners = [str(user.get("username") or "") for user in AuthManager().list_users()]
        owners = [name for name in owners if name]
    except Exception as error:
        logger.debug("Lotus: user enumeration failed: %s", error)
    return owners or [""]
