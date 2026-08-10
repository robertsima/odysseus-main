"""Authenticated, owner-isolated UI API for Lotus daily check-ins."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from src.auth_helpers import require_user
from src.lotus_checkins import LotusCheckinStore

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

    return router
