"""Authenticated, owner-isolated UI API for Lotus daily check-ins."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from src.auth_helpers import require_user
from src.lotus_checkins import LotusCheckinStore
from src.reminder_personas import PERSONAS

EmotionFamily = Literal[
    "pleasant_high",
    "pleasant_low",
    "unpleasant_high",
    "unpleasant_low",
]
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class CheckinCreate(BaseModel):
    occurred_at: datetime
    timezone: str | None = Field(default=None, max_length=64)
    emotion_label: str = Field(min_length=1, max_length=120)
    emotion_family: EmotionFamily
    valence: float = Field(ge=-1, le=1)
    energy: float = Field(ge=0, le=1)
    intensity: float = Field(ge=0, le=1)
    note: str | None = Field(default=None, max_length=8000)
    tags: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=32
    )
    context: dict[str, str] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value


class ReminderPreferences(BaseModel):
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    reminder_enabled: bool = False
    reminder_times: list[str] = Field(default_factory=list, max_length=8)
    reminder_weekdays: list[int] = Field(
        default_factory=lambda: list(range(7)), max_length=7
    )
    quiet_start: str | None = None
    quiet_end: str | None = None
    snooze_minutes: int = Field(default=30, ge=5, le=240)
    # `inherit` follows the app-wide reminder_channel so a user who switches
    # the global channel does not have to update Lotus separately.
    channel: Literal["inherit", "browser", "email", "ntfy", "webhook"] = "inherit"
    min_hours_between: int = Field(default=0, ge=0, le=168)
    skip_if_checked_in: bool = True
    # "plain" is the explicit no-LLM-phrasing option; any other value is a
    # persona id from src/reminder_personas.py.
    message_style: str = Field(default="plain", max_length=40)
    paused_until: str | None = None
    insights_enabled: bool = False
    insights_frequency: Literal["weekly", "biweekly", "monthly"] = "weekly"
    insights_weekday: int = Field(default=6, ge=0, le=6)
    insights_time: str = Field(default="09:00")

    @field_validator("insights_time")
    @classmethod
    def validate_insights_time(cls, value: str) -> str:
        if not _TIME_RE.fullmatch(value or ""):
            raise ValueError("insights time must use HH:MM")
        return value

    @field_validator("message_style")
    @classmethod
    def validate_message_style(cls, value: str) -> str:
        style = (value or "plain").strip().lower()
        if style != "plain" and style not in PERSONAS:
            raise ValueError("unknown message style")
        return style

    @field_validator("paused_until")
    @classmethod
    def validate_paused_until(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("paused_until must be an ISO timestamp") from None
        if moment.tzinfo is None:
            raise ValueError("paused_until must include a UTC offset")
        return moment.astimezone(UTC).isoformat(timespec="seconds")

    @field_validator("reminder_times")
    @classmethod
    def validate_times(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _TIME_RE.fullmatch(value):
                raise ValueError("reminder times must use HH:MM")
        return list(dict.fromkeys(values))

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def validate_optional_time(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _TIME_RE.fullmatch(value):
            raise ValueError("quiet hours must use HH:MM")
        return value

    @field_validator("reminder_weekdays")
    @classmethod
    def validate_weekdays(cls, values: list[int]) -> list[int]:
        if any(value < 0 or value > 6 for value in values):
            raise ValueError("weekdays must be between 0 and 6")
        return sorted(set(values))


class SnoozeRequest(BaseModel):
    minutes: int = Field(default=30, ge=5, le=10080)


class LotusAccessPolicy(BaseModel):
    local: bool = True
    lan: bool = True
    api: bool = False


def setup_lotus_routes() -> APIRouter:
    router = APIRouter(prefix="/api/lotus", tags=["lotus"])

    @router.get("/overview")
    def overview(owner: str = Depends(require_user)):
        store = LotusCheckinStore(owner)
        return {**store.overview(), "preferences": store.get_preferences()}

    @router.get("/checkins")
    def list_checkins(
        limit: int = Query(default=100, ge=1, le=365),
        before: str | None = None,
        owner: str = Depends(require_user),
    ):
        return {
            "checkins": LotusCheckinStore(owner).list_checkins(
                limit=limit, before=before
            )
        }

    @router.post("/checkins", status_code=201)
    def create_checkin(body: CheckinCreate, owner: str = Depends(require_user)):
        values = body.model_dump()
        values["note"] = values["note"].strip() if values.get("note") else None
        return LotusCheckinStore(owner).create_checkin(values)

    @router.delete("/checkins/{entry_id}")
    def delete_checkin(entry_id: str, owner: str = Depends(require_user)):
        if not LotusCheckinStore(owner).delete_checkin(entry_id):
            raise HTTPException(404, "Check-in not found")
        return {"status": "deleted", "id": entry_id}

    @router.get("/preferences")
    def get_preferences(owner: str = Depends(require_user)):
        return LotusCheckinStore(owner).get_preferences()

    @router.put("/preferences")
    def save_preferences(body: ReminderPreferences, owner: str = Depends(require_user)):
        return LotusCheckinStore(owner).save_preferences(body.model_dump())

    @router.get("/access-policy")
    def get_access_policy(owner: str = Depends(require_user)):
        from src.lotus_access import get_lotus_access_policy

        return get_lotus_access_policy(owner)

    @router.put("/access-policy")
    def save_access_policy(body: LotusAccessPolicy, owner: str = Depends(require_user)):
        from src.lotus_access import save_lotus_access_policy

        return save_lotus_access_policy(owner, body.model_dump())

    @router.post("/preferences/test")
    async def test_notification(owner: str = Depends(require_user)):
        """Fire one notification now, through the real delivery path.

        Deliberately not a simulation — a user who picks email/ntfy/webhook
        needs to find out here, not at 21:00, that the channel is misconfigured.
        """
        from src.lotus_notifications import send_test_notification

        return await send_test_notification(owner)

    @router.post("/snooze")
    def snooze(body: SnoozeRequest, owner: str = Depends(require_user)):
        store = LotusCheckinStore(owner)
        until = datetime.now(UTC) + timedelta(minutes=body.minutes)
        prefs = store.save_preferences(
            {"paused_until": until.isoformat(timespec="seconds")}
        )
        return {"paused_until": prefs["paused_until"]}

    @router.delete("/snooze")
    def clear_snooze(owner: str = Depends(require_user)):
        return {"paused_until": LotusCheckinStore(owner).save_preferences(
            {"paused_until": None}
        )["paused_until"]}

    @router.get("/notifications")
    def list_notifications(
        limit: int = Query(default=50, ge=1, le=200),
        owner: str = Depends(require_user),
    ):
        return {"notifications": LotusCheckinStore(owner).list_notifications(limit=limit)}

    @router.get("/insights")
    def insights(
        days: int = Query(default=30, ge=1, le=365),
        owner: str = Depends(require_user),
    ):
        from src.lotus_insights import compute_insights, render_observations

        payload = compute_insights(LotusCheckinStore(owner), days=days)
        return {**payload, "summary": render_observations(payload)}

    return router
