"""Dead environment reads and secret-in-environ writes removed on 2026-09-28.

* ``CLEANUP_ENABLED`` / ``CLEANUP_INTERVAL_HOURS`` were read into constants
  nothing imported.
* ``ODYSSEUS_FASTEMBED_LANE`` (``fastembed_lane_mode``) and the HTTP embedding
  client (``EMBEDDING_URL`` / ``_MODEL`` / ``_API_KEY`` / ``_BATCH_SIZE`` /
  ``_MAX_CHARS``) had no caller once retrieval became one FastEmbed lane.
* Saving an embedding endpoint wrote its decrypted API key into ``os.environ``,
  where every MCP server and shell the app spawns inherited it.
* ``ODYSSEUS_STT_ENABLED`` / ``_DEFAULT_PROVIDER`` / ``_MODEL`` were fallbacks
  behind settings that always exist.
"""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_cleanup_constants_are_gone():
    import src.constants as constants

    assert not hasattr(constants, "CLEANUP_ENABLED")
    assert not hasattr(constants, "CLEANUP_INTERVAL_HOURS")


def test_http_embedding_client_is_gone():
    import src.embedding_lanes as lanes
    import src.embeddings as embeddings

    for name in ("EmbeddingClient", "get_http_embedding_client", "reset_http_embed_state"):
        assert not hasattr(embeddings, name), name
    assert not hasattr(lanes, "_load_custom_endpoint")
    assert callable(embeddings.get_embedding_client)


def _embedding_app(tmp_path, monkeypatch):
    import routes.embedding_routes as routes
    import src.secret_storage as secret_storage
    from core.middleware import require_admin

    monkeypatch.setattr(routes, "_ENDPOINT_FILE", str(tmp_path / "embedding_endpoint.json"))
    monkeypatch.setattr(secret_storage, "encrypt", lambda value: "enc:" + value)

    class _Ok:
        def raise_for_status(self):
            return None

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Ok())
    app = FastAPI()
    app.include_router(routes.setup_embedding_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_saving_an_endpoint_keeps_the_key_out_of_the_environment(tmp_path, monkeypatch):
    for name in ("EMBEDDING_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = _embedding_app(tmp_path, monkeypatch)

    resp = client.post("/api/embeddings/endpoint", data={
        "url": "http://127.0.0.1:11434/v1/embeddings", "model": "nomic", "api_key": "sk-embed-secret",
    })

    assert resp.status_code == 200, resp.text
    for name in ("EMBEDDING_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"):
        assert name not in os.environ, name
    assert "sk-embed-secret" not in json.dumps(dict(os.environ))
    saved = json.loads((tmp_path / "embedding_endpoint.json").read_text(encoding="utf-8"))
    assert saved == {"url": "http://127.0.0.1:11434/v1/embeddings", "model": "nomic",
                     "api_key": "enc:sk-embed-secret"}


def test_endpoint_view_reads_only_the_saved_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_URL", "http://from-env/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_MODEL", "env-model")
    client = _embedding_app(tmp_path, monkeypatch)

    assert client.get("/api/embeddings/endpoint").json() == {"url": "", "model": "", "active": False}


def test_stt_ignores_the_retired_env_fallbacks_but_keeps_hardware_wiring(monkeypatch):
    import src.settings as settings_mod
    from services.stt.stt_service import STTService

    defaults = {"stt_enabled": True, "stt_provider": "local", "stt_model": "base", "stt_language": ""}
    monkeypatch.setattr(settings_mod, "get_user_setting",
                        lambda key, owner="", default=None: defaults.get(key, default))
    monkeypatch.setattr(settings_mod, "get_setting_or_env", lambda key, env, default=None: default)
    monkeypatch.setenv("ODYSSEUS_STT_ENABLED", "false")
    monkeypatch.setenv("ODYSSEUS_STT_DEFAULT_PROVIDER", "disabled")
    monkeypatch.setenv("ODYSSEUS_STT_MODEL", "large-v3")
    monkeypatch.setenv("ODYSSEUS_STT_DEVICE", "CUDA")
    monkeypatch.setenv("ODYSSEUS_STT_COMPUTE_TYPE", "float16")

    loaded = STTService()._load_settings("alice")

    assert (loaded["stt_enabled"], loaded["stt_provider"], loaded["stt_model"]) == (True, "local", "base")
    assert (loaded["stt_device"], loaded["stt_compute_type"]) == ("cuda", "float16")


def test_stt_setting_defaults_match_the_settings_defaults(monkeypatch):
    """The literal fallbacks that replaced the env reads are the same values
    DEFAULT_SETTINGS declares, so a missing key behaves as before."""
    import src.settings as settings_mod
    from services.stt.stt_service import STTService

    monkeypatch.setattr(settings_mod, "get_user_setting", lambda key, owner="", default=None: default)
    monkeypatch.setattr(settings_mod, "get_setting_or_env", lambda key, env, default=None: default)

    loaded = STTService()._load_settings()
    defaults = settings_mod.DEFAULT_SETTINGS
    assert loaded["stt_enabled"] == defaults["stt_enabled"]
    assert loaded["stt_provider"] == defaults["stt_provider"]
    assert loaded["stt_model"] == defaults["stt_model"]
