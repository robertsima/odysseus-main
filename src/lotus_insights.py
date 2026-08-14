"""Aggregate observations over an owner's Lotus data.

Everything here is deliberately descriptive. It counts what was logged, always
alongside the sample size that produced the number, and it never reads the
``note`` column — the statistics come from
``lotus_mcp.services.summary_service``, whose queries do not select notes at
all, so there is no path on which private text could reach a caller.

Nothing in this module diagnoses, advises, or interprets. It says what the
data contains and how thin it is.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

from src.lotus_checkins import LotusCheckinStore

_policy_module = importlib.import_module("lotus_mcp.policy")
_config_module = importlib.import_module("lotus_mcp.config")
_query_module = importlib.import_module("lotus_mcp.services.query_service")
_summary_module = importlib.import_module("lotus_mcp.services.summary_service")

PolicyEngine = _policy_module.PolicyEngine
PrivacyPolicySettings = _config_module.PrivacyPolicySettings
QueryService = _query_module.QueryService
SummaryService = _summary_module.SummaryService
local_time = _query_module.local_time

DISCLAIMER = (
    "These are counts of the user's own logged check-ins, not a clinical or "
    "psychological assessment."
)

#: Below this, a bucket is reported as a raw count and never compared.
MIN_BUCKET_ENTRIES = 3

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_PARTS = (
    ("early morning", 5, 9),
    ("morning", 9, 12),
    ("afternoon", 12, 17),
    ("evening", 17, 22),
    ("night", 22, 5),
)


def _services(store: LotusCheckinStore) -> tuple[Any, Any]:
    """Build the shipped Lotus statistics services against one owner's database.

    The default :class:`PrivacyPolicySettings` are used unchanged — aggregates
    and emotion labels on, raw entries and notes off.
    """
    policy = PolicyEngine(PrivacyPolicySettings())
    queries = QueryService(policy, store.database)
    return policy, SummaryService(policy, store.database, queries)


def _day_part(hour: int) -> str:
    for label, start, end in _DAY_PARTS:
        if start <= end:
            if start <= hour < end:
                return label
        elif hour >= start or hour < end:
            return label
    return "night"


def _extreme(buckets: list[dict[str, Any]], key: str, *, highest: bool) -> dict[str, Any] | None:
    """Pick the strongest bucket, ignoring any that is too thin to compare."""
    usable = [b for b in buckets if b.get(key) is not None and b["entries"] >= MIN_BUCKET_ENTRIES]
    if not usable:
        return None
    return (max if highest else min)(usable, key=lambda b: b[key])


def _bucketed(rows: list[Any], labeller) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        moment = local_time(row["occurred_at_utc"], row["utc_offset_minutes"])
        key = labeller(moment)
        bucket = grouped.setdefault(
            key, {"bucket": key, "entries": 0, "_energy": [], "_valence": []}
        )
        bucket["entries"] += 1
        if row["energy"] is not None:
            bucket["_energy"].append(float(row["energy"]))
        if row["valence"] is not None:
            bucket["_valence"].append(float(row["valence"]))
    result = []
    for bucket in grouped.values():
        energy, valence = bucket.pop("_energy"), bucket.pop("_valence")
        bucket["average_energy"] = round(sum(energy) / len(energy), 3) if energy else None
        bucket["average_valence"] = round(sum(valence) / len(valence), 3) if valence else None
        bucket["sparse"] = bucket["entries"] < MIN_BUCKET_ENTRIES
        result.append(bucket)
    return result


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _streak_days(rows: list[Any], today) -> int:
    """Consecutive local days ending today (or yesterday) with a check-in."""
    days = {local_time(row["occurred_at_utc"], row["utc_offset_minutes"]).date() for row in rows}
    if not days:
        return 0
    cursor = max(days)
    if (today - cursor).days > 1:
        return 0
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def compute_insights(
    store: LotusCheckinStore,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate observations over the last ``days`` days, with sample sizes."""
    days = max(1, min(int(days or 30), 365))
    end = (now or datetime.now(UTC)).astimezone(UTC)
    start = end - timedelta(days=days)
    previous_start = start - timedelta(days=days)
    policy, summaries = _services(store)
    queries = QueryService(policy, store.database)
    # The 90-day confirmation gate exists to stop a client probing the full
    # history; here the window is the number of days the owner asked for.
    confirm = days > policy.settings.max_query_days_without_confirmation

    current = summaries.summarize_period(
        start=start, end=end, group_by="day", confirm_extended_range=confirm
    )
    previous = summaries.summarize_period(
        start=previous_start, end=start, group_by="day", confirm_extended_range=confirm
    )
    rows = queries.fetch_for_aggregation(start=start, end=end)

    valence = [float(r["valence"]) for r in rows if r["valence"] is not None]
    energy = [float(r["energy"]) for r in rows if r["energy"] is not None]
    intensity = [float(r["intensity"]) for r in rows if r["intensity"] is not None]
    previous_rows = queries.fetch_for_aggregation(start=previous_start, end=start)
    previous_values = {
        field: [float(r[field]) for r in previous_rows if r[field] is not None]
        for field in ("valence", "energy", "intensity")
    }

    averages = {}
    trend = {}
    for field, values in (("valence", valence), ("energy", energy), ("intensity", intensity)):
        current_mean = _average(values)
        previous_mean = _average(previous_values[field])
        averages[field] = {"value": current_mean, "sample_size": len(values)}
        trend[field] = {
            "previous": previous_mean,
            "previous_sample_size": len(previous_values[field]),
            "change": (
                round(current_mean - previous_mean, 3)
                if current_mean is not None and previous_mean is not None
                else None
            ),
        }

    families: dict[str, int] = {}
    for row in rows:
        family = row["emotion_family"]
        if family:
            families[family] = families.get(family, 0) + 1

    time_of_day = _bucketed(rows, lambda moment: _day_part(moment.hour))
    weekday = _bucketed(rows, lambda moment: _WEEKDAYS[moment.weekday()])

    emotion_counts = current.get("emotion_counts") or {}
    cautions = list(current.get("cautions") or [])
    if len(rows) < MIN_BUCKET_ENTRIES:
        cautions.append(
            "Too few check-ins to describe anything as a pattern; treat these as raw counts."
        )
    if trend["energy"]["previous_sample_size"] == 0:
        cautions.append(
            f"The preceding {days}-day window holds no check-ins, so no comparison is possible."
        )

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "sample_size": len(rows),
        "checkin_count": len(rows),
        "days_with_checkins": len([b for b in current.get("buckets") or [] if b["entry_count"]]),
        "streak_days": _streak_days(rows, end.date()),
        "averages": averages,
        "trend": trend,
        "top_emotions": [
            {"label": label, "count": count}
            for label, count in sorted(emotion_counts.items(), key=lambda kv: -kv[1])[:5]
        ],
        "emotion_families": [
            {"family": family, "count": count}
            for family, count in sorted(families.items(), key=lambda kv: -kv[1])
        ],
        "time_of_day": {
            "buckets": sorted(time_of_day, key=lambda b: b["bucket"]),
            "highest_energy": _extreme(time_of_day, "average_energy", highest=True),
            "lowest_energy": _extreme(time_of_day, "average_energy", highest=False),
        },
        "weekday": {
            "buckets": sorted(weekday, key=lambda b: _WEEKDAYS.index(b["bucket"])),
            "highest_energy": _extreme(weekday, "average_energy", highest=True),
            "lowest_energy": _extreme(weekday, "average_energy", highest=False),
        },
        "missing_data": current.get("missing_data") or {},
        "cautions": cautions,
        "minimum_bucket_entries": MIN_BUCKET_ENTRIES,
        "disclaimer": DISCLAIMER,
        "notes_accessed": False,
    }


def render_observations(payload: dict[str, Any], *, limit: int = 5) -> str:
    """Deterministic plain-text body for a notification or a chat reply."""
    total = payload.get("sample_size") or 0
    days = (payload.get("range") or {}).get("days", 30)
    if not total:
        return (
            f"No check-ins logged in the last {days} days, so there is nothing to observe yet. "
            + DISCLAIMER
        )

    lines = [f"Over the last {days} days you logged {total} check-in{'s' if total != 1 else ''}."]
    streak = payload.get("streak_days") or 0
    if streak > 1:
        lines.append(f"Current run: {streak} consecutive days with a check-in.")

    for field, label in (("energy", "energy"), ("valence", "pleasantness")):
        stats = (payload.get("averages") or {}).get(field) or {}
        if stats.get("value") is None:
            continue
        line = f"Average {label}: {stats['value']} across {stats['sample_size']} check-ins"
        change = ((payload.get("trend") or {}).get(field) or {}).get("change")
        if change is not None:
            direction = "higher" if change > 0 else "lower" if change < 0 else "unchanged"
            line += f" ({abs(change)} {direction} than the previous {days} days)"
        lines.append(line + ".")

    top = payload.get("top_emotions") or []
    if top:
        listed = ", ".join(f"{item['label']} ({item['count']})" for item in top[:limit])
        lines.append(f"Most logged words: {listed}.")

    for key, label in (("time_of_day", "time of day"), ("weekday", "weekday")):
        section = payload.get(key) or {}
        high, low = section.get("highest_energy"), section.get("lowest_energy")
        if high and low and high["bucket"] != low["bucket"]:
            lines.append(
                f"Highest average energy by {label}: {high['bucket']} "
                f"({high['average_energy']} over {high['entries']} check-ins); "
                f"lowest: {low['bucket']} ({low['average_energy']} over {low['entries']})."
            )

    cautions = payload.get("cautions") or []
    if cautions:
        lines.append("Caveats: " + " ".join(cautions[:3]))
    lines.append(DISCLAIMER)
    return "\n".join(lines)
