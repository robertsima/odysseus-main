"""Authenticated, owner-scoped speech-to-text routes."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from services.stt.stt_service import STTError
from src.auth_helpers import owner_filter, require_user
from src.model_context import is_local_endpoint
from src.upload_limits import STT_MAX_AUDIO_BYTES, read_upload_limited

logger = logging.getLogger(__name__)

_AUDIO_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/m4a",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
}


class STTPreferences(BaseModel):
    enabled: bool = True
    provider: str = Field(default="local", min_length=1, max_length=160)
    model: str = Field(default="base", min_length=1, max_length=160)
    language: str = Field(default="", max_length=32)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        value = value.strip()
        if value in {"disabled", "browser", "local"} or value.startswith("endpoint:"):
            return value
        raise ValueError("Unsupported STT provider")


def _prefs_owner(owner: str) -> str | None:
    return owner or None


def setup_stt_routes(stt_service) -> APIRouter:
    router = APIRouter(prefix="/api/stt", tags=["stt"])

    @router.get("/status")
    @router.get("/stats")
    async def get_stt_status(owner: Annotated[str, Depends(require_user)]):
        return stt_service.get_stats(owner)

    @router.get("/preferences")
    async def get_preferences(owner: Annotated[str, Depends(require_user)]):
        settings = stt_service._load_settings(owner)
        return {
            "enabled": settings["stt_enabled"],
            "provider": settings["stt_provider"],
            "model": settings["stt_model"],
            "language": settings["stt_language"],
        }

    @router.put("/preferences")
    async def save_preferences(
        body: STTPreferences, owner: Annotated[str, Depends(require_user)]
    ):
        from routes.prefs_routes import _load_for_user, _save_for_user

        if body.provider.startswith("endpoint:"):
            endpoint_id = body.provider.split(":", 1)[1]
            if stt_service._resolve_endpoint(endpoint_id, owner) is None:
                raise HTTPException(404, "Transcription endpoint not found")
        prefs = _load_for_user(_prefs_owner(owner))
        prefs.update(
            {
                "stt_enabled": body.enabled,
                "stt_provider": body.provider,
                "stt_model": body.model,
                "stt_language": body.language.strip(),
            }
        )
        _save_for_user(_prefs_owner(owner), prefs)
        return body.model_dump()

    @router.get("/providers")
    async def list_providers(owner: Annotated[str, Depends(require_user)]):
        from src.database import ModelEndpoint, SessionLocal

        providers = [
            {
                "id": "local",
                "name": "Local Whisper",
                "privacy": "local",
                "available": stt_service.get_stats(owner)["local_installed"],
            },
            {
                "id": "browser",
                "name": "Browser speech service",
                "privacy": "browser-service",
                "available": True,
            },
        ]
        db = SessionLocal()
        try:
            query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled.is_(True))
            endpoints = owner_filter(query, ModelEndpoint, owner).all()
            providers.extend(
                {
                    "id": f"endpoint:{endpoint.id}",
                    "name": endpoint.name,
                    "privacy": (
                        "local"
                        if is_local_endpoint(endpoint.base_url or "")
                        else "hosted"
                    ),
                    "available": True,
                }
                for endpoint in endpoints
            )
        finally:
            db.close()
        return {"providers": providers}

    @router.post("/transcribe")
    async def transcribe_audio(
        file: Annotated[UploadFile, File()],
        owner: Annotated[str, Depends(require_user)],
    ):
        mime_type = (file.content_type or "").split(";", 1)[0].lower()
        if mime_type not in _AUDIO_TYPES:
            raise HTTPException(
                415,
                {
                    "code": "unsupported_audio_type",
                    "message": "Use WebM, Ogg, MP4/M4A, MP3, or WAV audio.",
                },
            )
        audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
        if not audio_bytes:
            raise HTTPException(
                400, {"code": "empty_audio", "message": "Audio file is empty."}
            )
        try:
            result = await run_in_threadpool(
                stt_service.transcribe,
                audio_bytes,
                owner=owner,
                filename=file.filename or "audio",
                mime_type=mime_type,
            )
        except STTError as exc:
            raise HTTPException(
                exc.status_code, {"code": exc.code, "message": exc.message}
            ) from exc
        if result is None:
            raise HTTPException(
                503,
                {
                    "code": "stt_unavailable",
                    "message": "Speech-to-text is disabled or unavailable.",
                },
            )
        return result

    return router
