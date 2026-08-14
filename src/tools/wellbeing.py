"""Wellbeing-domain tool implementation (Lotus).

Holds `manage_wellbeing`, the only way a model reaches Lotus data outside the
MCP surface. Two rules govern everything here:

* Private notes never leave the store. Summary/pattern payloads come from
  `src.lotus_insights`, whose queries never select the `note` column; the
  `latest` action explicitly drops it as well.
* Owner-approved endpoints only. `src.agent_loop._apply_private_mcp_filter`
  applies the local/LAN/API policy before prompting; `is_local_session`
  re-checks it at execution time so a stale tool call cannot bypass it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Dict, Optional

from src.tools._common import _parse_tool_args

logger = logging.getLogger(__name__)

REMOTE_ENDPOINT_REFUSAL = (
    "Wellbeing data is private and this conversation's endpoint is not allowed "
    "by the owner's Lotus access policy, so manage_wellbeing returned nothing. "
    "The user can change this under Settings > Privacy > Lotus model access."
)

_EMOTION_FAMILIES = ("pleasant_high", "pleasant_low", "unpleasant_high", "unpleasant_low")

_ACTION_ALIASES = {
    "aggregate": "summary",
    "summarize": "summary",
    "overview": "summary",
    "trends": "summary",
    "energy": "patterns",
    "pattern": "patterns",
    "schedule": "patterns",
    "last": "latest",
    "recent": "latest",
    "prefs": "preferences",
    "settings": "preferences",
    "checkin": "log_checkin",
    "check_in": "log_checkin",
    "log": "log_checkin",
    "add": "log_checkin",
}


def is_local_session(session_id: Optional[str], owner: Optional[str] = None) -> bool:
    """Whether the endpoint serving this session is allowed for Lotus.

    Fails closed: an unresolvable session cannot be shown to be local, so it
    is treated as remote. Same rule and same helper as the private-RAG gate.
    """
    if not session_id:
        return False
    try:
        from src import ai_interaction
        from src.lotus_access import lotus_endpoint_allowed

        manager = getattr(ai_interaction, "_session_manager", None)
        if manager is None:
            return False
        session = manager.get_session(session_id)
        if not session:
            return False
        return lotus_endpoint_allowed(owner, getattr(session, "endpoint_url", "") or "")
    except Exception:
        return False


def _sample_note(payload: Dict) -> Dict:
    """Attach the caller-facing reporting rules to every returned payload."""
    payload["reporting_rules"] = (
        "Report these as observations of the user's own logged check-ins, always "
        "with the sample size. Do not diagnose, do not give clinical or "
        "psychological advice, and do not speculate about causes."
    )
    return payload


async def do_manage_wellbeing(
    content: str,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict:
    """Handle manage_wellbeing tool calls: aggregate mood/energy observations."""
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    raw_action = str(args.get("action") or "summary").replace("-", "_").strip().lower()
    action = _ACTION_ALIASES.get(raw_action, raw_action)

    try:
        from src.lotus_checkins import LotusCheckinStore
        from src.lotus_insights import compute_insights, render_observations

        store = LotusCheckinStore(owner or "")

        if action == "summary":
            days = max(1, min(int(args.get("days") or 30), 365))
            payload = compute_insights(store, days=days)
            return _sample_note(
                {
                    "action": "summary",
                    "range": payload["range"],
                    "sample_size": payload["sample_size"],
                    "streak_days": payload["streak_days"],
                    "averages": payload["averages"],
                    "trend": payload["trend"],
                    "top_emotions": payload["top_emotions"],
                    "emotion_families": payload["emotion_families"],
                    "cautions": payload["cautions"],
                    "disclaimer": payload["disclaimer"],
                    "summary": render_observations(payload),
                    "exit_code": 0,
                }
            )

        if action == "patterns":
            days = max(1, min(int(args.get("days") or 30), 365))
            payload = compute_insights(store, days=days)
            return _sample_note(
                {
                    "action": "patterns",
                    "range": payload["range"],
                    "sample_size": payload["sample_size"],
                    "time_of_day": payload["time_of_day"],
                    "weekday": payload["weekday"],
                    "minimum_bucket_entries": payload["minimum_bucket_entries"],
                    "cautions": payload["cautions"],
                    "disclaimer": payload["disclaimer"],
                    "planning_hint": (
                        "Buckets below minimum_bucket_entries are too thin to schedule "
                        "against; say so rather than treating them as a pattern."
                    ),
                    "exit_code": 0,
                }
            )

        if action == "latest":
            overview = store.overview()
            last = overview.get("last_checkin") or None
            return _sample_note(
                {
                    "action": "latest",
                    "total_checkins": overview.get("total_checkins", 0),
                    "last_30_days": overview.get("last_30_days", 0),
                    "averages_30_days": overview.get("averages_30_days", {}),
                    # Note text is deliberately dropped here — the raw row
                    # carries it, the model must never see it.
                    "latest": None
                    if not last
                    else {
                        "occurred_at": last["occurred_at"],
                        "emotion_label": last["emotion_label"],
                        "emotion_family": last["emotion_family"],
                        "valence": last["valence"],
                        "energy": last["energy"],
                        "intensity": last["intensity"],
                        "tags": last["tags"],
                    },
                    "disclaimer": "A single check-in is one data point, not a trend.",
                    "exit_code": 0,
                }
            )

        if action == "preferences":
            prefs = dict(store.get_preferences())
            return {"action": "preferences", "preferences": prefs, "exit_code": 0}

        if action == "log_checkin":
            family = str(args.get("emotion_family") or "").strip().lower()
            if family not in _EMOTION_FAMILIES:
                return {
                    "error": f"emotion_family must be one of: {', '.join(_EMOTION_FAMILIES)}",
                    "exit_code": 1,
                }
            label = str(args.get("emotion_label") or "").strip()
            if not label:
                return {"error": "emotion_label is required", "exit_code": 1}
            occurred_at = str(args.get("occurred_at") or "").strip()
            try:
                moment = (
                    datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(UTC)
                )
            except ValueError:
                return {"error": "occurred_at must be an ISO timestamp", "exit_code": 1}
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)

            def _clamp(value, low, high, default):
                try:
                    return max(low, min(high, float(value)))
                except (TypeError, ValueError):
                    return default

            defaults = {
                "pleasant_high": (0.7, 0.8),
                "pleasant_low": (0.6, 0.25),
                "unpleasant_high": (-0.7, 0.8),
                "unpleasant_low": (-0.65, 0.2),
            }[family]
            created = store.create_checkin(
                {
                    "occurred_at": moment,
                    "timezone": str(args.get("timezone") or "") or None,
                    "emotion_label": label,
                    "emotion_family": family,
                    "valence": _clamp(args.get("valence"), -1, 1, defaults[0]),
                    "energy": _clamp(args.get("energy"), 0, 1, defaults[1]),
                    "intensity": _clamp(args.get("intensity"), 0, 1, 0.5),
                    "note": (str(args.get("note")).strip() or None) if args.get("note") else None,
                    "tags": [str(tag).strip() for tag in (args.get("tags") or []) if str(tag).strip()],
                    # Marks the row as model-entered so UI check-ins and
                    # assistant-entered ones stay distinguishable.
                    "context": {"entry_method": "model_tool", "tool": "manage_wellbeing"},
                }
            )
            return {
                "action": "log_checkin",
                "status": "saved",
                "id": created["id"],
                "occurred_at": created["occurred_at"],
                "emotion_label": created["emotion_label"],
                "emotion_family": created["emotion_family"],
                "exit_code": 0,
            }

        return {
            "error": (
                "Unknown action. Use summary, patterns, latest, preferences, or log_checkin."
            ),
            "exit_code": 1,
        }
    except Exception as e:
        logger.error(f"manage_wellbeing error: {e}")
        return {"error": str(e), "exit_code": 1}
