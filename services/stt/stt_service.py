"""Owner-scoped multi-provider speech-to-text service."""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import httpx

from src.auth_helpers import owner_filter
from src.model_context import is_local_endpoint

logger = logging.getLogger(__name__)


class STTError(Exception):
    """A stable, user-safe transcription failure."""

    def __init__(self, code: str, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class STTService:
    """Dispatch local Whisper and owner-visible OpenAI-compatible endpoints."""

    def __init__(self) -> None:
        self._whisper_models: dict[tuple[str, str, str], Any] = {}
        self._model_lock = threading.Lock()
        self._inference_slots = threading.BoundedSemaphore(
            max(1, int(os.getenv("ODYSSEUS_STT_CONCURRENCY", "1") or 1))
        )

    def _load_settings(self, owner: str = "") -> dict[str, Any]:
        from src.settings import get_user_setting

        env_enabled = os.getenv("ODYSSEUS_STT_ENABLED", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        return {
            "stt_enabled": bool(get_user_setting("stt_enabled", owner, env_enabled)),
            "stt_provider": str(
                get_user_setting(
                    "stt_provider",
                    owner,
                    os.getenv("ODYSSEUS_STT_DEFAULT_PROVIDER", "local"),
                )
                or "disabled"
            ),
            "stt_model": str(
                get_user_setting(
                    "stt_model", owner, os.getenv("ODYSSEUS_STT_MODEL", "base")
                )
                or "base"
            ),
            "stt_language": str(
                get_user_setting("stt_language", owner, "") or ""
            ).strip(),
            "stt_device": os.getenv("ODYSSEUS_STT_DEVICE", "cpu").strip().lower(),
            "stt_compute_type": os.getenv("ODYSSEUS_STT_COMPUTE_TYPE", "int8").strip(),
            "stt_beam_size": max(1, int(os.getenv("ODYSSEUS_STT_BEAM_SIZE", "1") or 1)),
            "stt_max_audio_seconds": max(
                1, int(os.getenv("ODYSSEUS_STT_MAX_AUDIO_SECONDS", "300") or 300)
            ),
        }

    def _settings_for(self, owner: str = "") -> dict[str, Any]:
        # Keep the no-owner call compatible with extensions that decorated the
        # original zero-argument settings loader.
        return self._load_settings(owner) if owner else self._load_settings()

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        return self._is_available(settings, "")

    def is_available(self, owner: str = "") -> bool:
        settings = self._settings_for(owner)
        return self._is_available(settings, owner)

    def _is_available(self, settings: dict[str, Any], owner: str) -> bool:
        if not settings["stt_enabled"]:
            return False
        provider = settings["stt_provider"]
        if provider in {"disabled", "browser"}:
            return False
        if provider == "local":
            return importlib.util.find_spec("faster_whisper") is not None
        if provider.startswith("endpoint:"):
            return self._resolve_endpoint(provider.split(":", 1)[1], owner) is not None
        return False

    def _model_key(self, settings: dict[str, Any]) -> tuple[str, str, str]:
        return (
            settings["stt_model"],
            settings["stt_device"],
            settings["stt_compute_type"],
        )

    def _get_whisper(self, settings: dict[str, Any] | None = None):
        settings = settings or self._load_settings()
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None

        key = self._model_key(settings)
        if key in self._whisper_models:
            return self._whisper_models[key]
        with self._model_lock:
            if key in self._whisper_models:
                return self._whisper_models[key]
            try:
                model = WhisperModel(key[0], device=key[1], compute_type=key[2])
            except Exception as exc:
                logger.error("Failed to load local STT model (%s)", type(exc).__name__)
                raise STTError(
                    "model_load_failed",
                    "The local speech model could not be loaded. Check the STT model and device settings.",
                    503,
                ) from exc
            self._whisper_models[key] = model
            logger.info("Local STT model loaded: model=%s device=%s", key[0], key[1])
            return model

    @staticmethod
    def _suffix(filename: str, mime_type: str) -> str:
        suffix = Path(filename or "").suffix.lower()
        if suffix in {".webm", ".ogg", ".mp4", ".m4a", ".mp3", ".wav", ".mpeg"}:
            return suffix
        return {
            "audio/webm": ".webm",
            "audio/ogg": ".ogg",
            "audio/mp4": ".mp4",
            "audio/m4a": ".m4a",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
        }.get(mime_type, ".audio")

    def _transcribe_local(
        self,
        audio_bytes: bytes,
        language: str = "",
        *,
        filename: str = "audio.webm",
        mime_type: str = "audio/webm",
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        supplied_settings = settings is not None
        settings = settings or self._load_settings()
        model = (
            self._get_whisper(settings) if supplied_settings else self._get_whisper()
        )
        if model is None:
            raise STTError(
                "local_dependency_missing",
                "Local speech-to-text is not installed in this build.",
                503,
            )
        tmp_path: str | None = None
        acquired = self._inference_slots.acquire(timeout=5)
        if not acquired:
            raise STTError("busy", "Speech-to-text is busy. Try again shortly.", 429)
        try:
            with tempfile.NamedTemporaryFile(
                suffix=self._suffix(filename, mime_type), delete=False
            ) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            kwargs: dict[str, Any] = {
                "beam_size": settings["stt_beam_size"],
                "vad_filter": True,
            }
            if language:
                kwargs["language"] = language
            segments, info = model.transcribe(tmp_path, **kwargs)
            text = " ".join(segment.text.strip() for segment in segments).strip()
            return {
                "text": text,
                "provider": "local",
                "model": settings["stt_model"],
                "privacy": "local",
                "language": getattr(info, "language", language or None),
                "duration_seconds": getattr(info, "duration", None),
            }
        except STTError:
            raise
        except Exception as exc:
            logger.error("Local STT failed (%s)", type(exc).__name__)
            raise STTError(
                "transcription_failed", "Local transcription failed.", 422
            ) from exc
        finally:
            self._inference_slots.release()
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    @staticmethod
    def _resolve_endpoint(endpoint_id: str, owner: str):
        from src.database import ModelEndpoint, SessionLocal

        db = SessionLocal()
        try:
            query = db.query(ModelEndpoint).filter(
                ModelEndpoint.id == endpoint_id,
                ModelEndpoint.is_enabled.is_(True),
            )
            endpoint = owner_filter(query, ModelEndpoint, owner).first()
            if endpoint is None:
                return None
            return {
                "id": endpoint.id,
                "name": endpoint.name,
                "base_url": endpoint.base_url,
                "api_key": endpoint.api_key,
                "privacy": (
                    "local" if is_local_endpoint(endpoint.base_url or "") else "hosted"
                ),
            }
        finally:
            db.close()

    def _transcribe_api(
        self,
        audio_bytes: bytes,
        endpoint_id: str,
        model: str,
        language: str = "",
        *,
        owner: str = "",
        filename: str = "audio.webm",
        mime_type: str = "audio/webm",
    ) -> dict[str, Any]:
        endpoint = self._resolve_endpoint(endpoint_id, owner)
        if endpoint is None:
            raise STTError(
                "endpoint_not_found",
                "The selected transcription endpoint is unavailable.",
                404,
            )
        url = endpoint["base_url"].rstrip("/") + "/audio/transcriptions"
        headers = {}
        if endpoint["api_key"]:
            headers["Authorization"] = f"Bearer {endpoint['api_key']}"
        files = {"file": (filename or "audio", io.BytesIO(audio_bytes), mime_type)}
        data = {"model": model or "whisper-1"}
        if language:
            data["language"] = language
        try:
            response = httpx.post(
                url, headers=headers, files=files, data=data, timeout=90
            )
            response.raise_for_status()
            text = str(response.json().get("text", "") or "").strip()
        except Exception as exc:
            logger.error("Hosted STT failed (%s)", type(exc).__name__)
            raise STTError(
                "endpoint_failed", "The transcription endpoint failed.", 502
            ) from exc
        return {
            "text": text,
            "provider": f"endpoint:{endpoint_id}",
            "model": model or "whisper-1",
            "privacy": endpoint["privacy"],
            "language": language or None,
            "duration_seconds": None,
        }

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        owner: str = "",
        filename: str = "audio.webm",
        mime_type: str = "audio/webm",
    ) -> dict[str, Any] | None:
        settings = self._settings_for(owner)
        if not settings["stt_enabled"]:
            return None
        try:
            import av

            with av.open(io.BytesIO(audio_bytes)) as container:
                duration = (
                    float(container.duration * av.time_base)
                    if container.duration is not None
                    else None
                )
            if duration and duration > settings["stt_max_audio_seconds"]:
                raise STTError(
                    "audio_too_long",
                    f"Recording exceeds the {settings['stt_max_audio_seconds']} second limit.",
                    413,
                )
        except STTError:
            raise
        except (ImportError, OSError, ValueError):
            # The selected decoder produces the useful format error later. The
            # duration probe must never make an otherwise valid codec fail.
            pass
        provider = settings["stt_provider"]
        if provider in {"disabled", "browser"}:
            return None
        if provider == "local":
            return self._transcribe_local(
                audio_bytes,
                settings["stt_language"],
                filename=filename,
                mime_type=mime_type,
                settings=settings,
            )
        if provider.startswith("endpoint:"):
            return self._transcribe_api(
                audio_bytes,
                provider.split(":", 1)[1],
                settings["stt_model"],
                settings["stt_language"],
                owner=owner,
                filename=filename,
                mime_type=mime_type,
            )
        raise STTError("invalid_provider", "Unknown transcription provider.", 400)

    def get_stats(self, owner: str = "") -> dict[str, Any]:
        settings = self._settings_for(owner)
        provider = settings["stt_provider"] if settings["stt_enabled"] else "disabled"
        local_installed = importlib.util.find_spec("faster_whisper") is not None
        result = {
            "available": self.is_available(owner),
            "enabled": settings["stt_enabled"],
            "provider": provider,
            "model": settings["stt_model"],
            "language": settings["stt_language"],
            "local_installed": local_installed,
            "model_loaded": self._model_key(settings) in self._whisper_models,
            "requires_https": False,
        }
        if provider.startswith("endpoint:"):
            endpoint = self._resolve_endpoint(provider.split(":", 1)[1], owner)
            result["privacy"] = endpoint["privacy"] if endpoint else "unavailable"
        elif provider == "browser":
            result["privacy"] = "browser-service"
        else:
            result["privacy"] = "local"
        return result


_stt_service: STTService | None = None


def get_stt_service() -> STTService:
    global _stt_service
    if _stt_service is None:
        _stt_service = STTService()
    return _stt_service
