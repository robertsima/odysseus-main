"""Aggregate summaries and low-energy pattern observation.

Nothing in this module diagnoses. It counts check-ins and reports what the
imported data contains, always alongside the sample size that produced the
number, and it declines to characterise a bucket that is too thin to mean
anything.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..database import Database
from ..policy import PolicyEngine
from .query_service import QueryService, local_time

__all__ = ["GROUP_BY_CHOICES", "SummaryService"]

GROUP_BY_CHOICES = ("day", "week", "month", "hour_of_day", "day_of_week")
PATTERN_GROUP_BY_CHOICES = ("hour_of_day", "day_of_week")

#: Below this many entries in a bucket, counts are reported but no comparative
#: statement is made about them.
SPARSE_BUCKET_THRESHOLD = 3

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_NOT_DIAGNOSTIC = (
    "These are counts of imported self-reported check-ins, not a clinical or "
    "psychological assessment."
)


def _bucket_key(moment: datetime, group_by: str) -> str:
    if group_by == "day":
        return moment.date().isoformat()
    if group_by == "week":
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    if group_by == "month":
        return f"{moment.year:04d}-{moment.month:02d}"
    if group_by == "hour_of_day":
        return f"{moment.hour:02d}"
    if group_by == "day_of_week":
        return _WEEKDAYS[moment.weekday()]
    raise ValueError(f"Unsupported group_by: {group_by}")


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


class SummaryService:
    def __init__(self, policy: PolicyEngine, database: Database, queries: QueryService) -> None:
        self.policy = policy
        self.db = database
        self.queries = queries

    # ---- period summary ----------------------------------------------------

    def summarize_period(
        self,
        *,
        start: datetime,
        end: datetime,
        group_by: str = "day",
        include_emotion_counts: bool = True,
        include_note_themes: bool = False,
        confirm_extended_range: bool = False,
    ) -> dict[str, Any]:
        self.policy.require_aggregate_summaries()
        if group_by not in GROUP_BY_CHOICES:
            raise ValueError(f"group_by must be one of: {', '.join(GROUP_BY_CHOICES)}")
        # Raises when themes are requested but not permitted; never silently
        # downgrades the request.
        themes_allowed = self.policy.resolve_note_themes(include_note_themes)
        start, end = self.policy.validate_range(start, end, confirmed=confirm_extended_range)

        rows = self.queries.fetch_for_aggregation(start=start, end=end)

        buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "entries": 0,
                "valence": [],
                "energy": [],
                "intensity": [],
                "labels": Counter(),
            }
        )
        label_counts: Counter[str] = Counter()
        missing = {"valence": 0, "energy": 0, "intensity": 0, "emotion_label": 0}

        for row in rows:
            moment = local_time(row["occurred_at_utc"], row["utc_offset_minutes"])
            bucket = buckets[_bucket_key(moment, group_by)]
            bucket["entries"] += 1
            for field in ("valence", "energy", "intensity"):
                value = row[field]
                if value is None:
                    missing[field] += 1
                else:
                    bucket[field].append(float(value))
            label = row["emotion_label"]
            if label:
                bucket["labels"][label] += 1
                label_counts[label] += 1
            else:
                missing["emotion_label"] += 1

        expose_labels = self.policy.settings.expose_emotion_labels and include_emotion_counts

        summary_buckets = []
        for key in sorted(buckets):
            data = buckets[key]
            item: dict[str, Any] = {
                "bucket": key,
                "entry_count": data["entries"],
                "average_valence": _mean(data["valence"]),
                "average_energy": _mean(data["energy"]),
                "average_intensity": _mean(data["intensity"]),
                "sparse": data["entries"] < SPARSE_BUCKET_THRESHOLD,
            }
            if expose_labels:
                item["emotion_counts"] = dict(data["labels"].most_common())
            summary_buckets.append(item)

        cautions = self._cautions(len(rows), missing, summary_buckets)

        result: dict[str, Any] = {
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "group_by": group_by,
            "total_entries": len(rows),
            "buckets": summary_buckets,
            "missing_data": missing,
            "cautions": cautions,
            "notes_accessed": False,
            "disclaimer": _NOT_DIAGNOSTIC,
        }
        if expose_labels:
            result["emotion_counts"] = dict(label_counts.most_common())
        if include_note_themes:
            # Reachable only when policy permitted it. Local-only theme
            # extraction is a designed extension point, not yet built — say so
            # rather than returning something derived from another source.
            result["note_themes"] = None
            result["note_themes_status"] = (
                "not_implemented" if themes_allowed else "denied_by_policy"
            )
        return result

    @staticmethod
    def _cautions(total: int, missing: dict[str, int], buckets: list[dict[str, Any]]) -> list[str]:
        cautions: list[str] = []
        if total == 0:
            cautions.append("No imported entries fall inside this range.")
            return cautions
        if total < SPARSE_BUCKET_THRESHOLD:
            cautions.append(
                f"Only {total} entries in range; treat every figure below as anecdotal."
            )
        for field in ("valence", "energy", "intensity"):
            if missing[field] == total:
                cautions.append(f"No entry in this range has a recorded {field}.")
            elif missing[field]:
                cautions.append(
                    f"{missing[field]} of {total} entries have no {field} value; "
                    f"averages use the remaining {total - missing[field]}."
                )
        sparse = sum(1 for bucket in buckets if bucket["sparse"])
        if sparse:
            cautions.append(
                f"{sparse} of {len(buckets)} buckets hold fewer than "
                f"{SPARSE_BUCKET_THRESHOLD} entries."
            )
        cautions.append(
            "Imported check-ins reflect when the user chose to log a mood, not the whole period."
        )
        return cautions

    # ---- low-energy observation -------------------------------------------

    def detect_low_energy_patterns(
        self,
        *,
        lookback_days: int = 30,
        energy_threshold: float = 0.3,
        valence_threshold: float = -0.3,
        minimum_entries: int = 3,
        group_by: str | None = None,
        now: datetime | None = None,
        confirm_extended_range: bool = False,
    ) -> dict[str, Any]:
        """Report where low-energy check-ins cluster, with the sample size.

        A bucket is only described in words when it clears ``minimum_entries``;
        everything else is returned as raw counts so the caller can see the
        data is thin rather than reading a confident sentence built on two
        records.
        """
        self.policy.require_aggregate_summaries()
        if group_by is not None and group_by not in PATTERN_GROUP_BY_CHOICES:
            raise ValueError(f"group_by must be one of: {', '.join(PATTERN_GROUP_BY_CHOICES)}")
        days = self.policy.clamp_lookback_days(
            lookback_days, hard_max=365, confirmed=confirm_extended_range
        )
        end = now or datetime.now(UTC)
        start = end - timedelta(days=days)

        rows = self.queries.fetch_for_aggregation(start=start, end=end)
        total = len(rows)

        scored = 0  # entries that carry at least one of the two dimensions
        matches = 0
        bucket_totals: Counter[str] = Counter()
        bucket_matches: Counter[str] = Counter()
        grouping = group_by or "day_of_week"

        for row in rows:
            energy, valence = row["energy"], row["valence"]
            if energy is None and valence is None:
                continue
            scored += 1
            moment = local_time(row["occurred_at_utc"], row["utc_offset_minutes"])
            key = _bucket_key(moment, grouping)
            bucket_totals[key] += 1
            # "Low" means every dimension the entry actually has is at or below
            # its threshold — a missing dimension neither qualifies nor
            # disqualifies the entry on its own.
            low_energy = energy is not None and energy <= energy_threshold
            low_valence = valence is not None and valence <= valence_threshold
            available = [d for d in (energy, valence) if d is not None]
            qualifies = (
                (low_energy and low_valence) if len(available) == 2 else (low_energy or low_valence)
            )
            if qualifies:
                matches += 1
                bucket_matches[key] += 1

        buckets = [
            {
                "bucket": key,
                "entries": bucket_totals[key],
                "low_energy_entries": bucket_matches.get(key, 0),
                "share": round(bucket_matches.get(key, 0) / bucket_totals[key], 3)
                if bucket_totals[key]
                else None,
                "meets_minimum_entries": bucket_matches.get(key, 0) >= minimum_entries,
            }
            for key in sorted(bucket_totals)
        ]

        observations: list[str] = []
        limitations: list[str] = []

        if total == 0:
            limitations.append(
                f"No imported entries in the last {days} days, so nothing can be observed."
            )
        elif scored == 0:
            limitations.append(
                "No entry in this window carries an energy or pleasantness value, "
                "so low-energy check-ins cannot be identified."
            )
        else:
            qualifying = [b for b in buckets if b["meets_minimum_entries"]]
            if not qualifying:
                limitations.append(
                    f"No {grouping.replace('_', ' ')} bucket reached the minimum of "
                    f"{minimum_entries} low-energy check-ins, so no pattern is reported."
                )
            for bucket in sorted(qualifying, key=lambda b: b["low_energy_entries"], reverse=True)[
                :3
            ]:
                observations.append(
                    f"The imported entries contain {bucket['low_energy_entries']} low-energy "
                    f"check-ins on {bucket['bucket']} out of {bucket['entries']} check-ins "
                    f"logged in that bucket."
                )
            if scored < total:
                limitations.append(
                    f"{total - scored} of {total} entries had neither an energy nor a "
                    "pleasantness value and were excluded."
                )

        limitations.append(
            "Check-ins are logged when the user chooses to log them; gaps are not evidence "
            "of anything in particular."
        )

        return {
            "lookback_days": days,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "group_by": grouping,
            "thresholds": {
                "energy_at_or_below": energy_threshold,
                "valence_at_or_below": valence_threshold,
                "minimum_entries": minimum_entries,
            },
            "sample_size": {
                "entries_in_window": total,
                "entries_with_energy_or_valence": scored,
                "low_energy_entries": matches,
            },
            "buckets": buckets,
            "observations": observations,
            "limitations": limitations,
            "notes_accessed": False,
            "disclaimer": _NOT_DIAGNOSTIC,
        }
