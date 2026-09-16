"""Settings are canonical for migrated choices, with env compatibility fallback."""

import json

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    import src.settings as settings

    settings._settings_cache = None


def _saved_settings(tmp_path, monkeypatch, values):
    import src.settings as settings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(path))
    settings._settings_cache = None


def test_saved_setting_wins_over_legacy_env(tmp_path, monkeypatch):
    from src.settings import get_setting_or_env

    _saved_settings(tmp_path, monkeypatch, {"agent_base_branch": "release"})
    monkeypatch.setenv("ODYSSEUS_AGENT_BASE_BRANCH", "main")

    assert get_setting_or_env("agent_base_branch", "ODYSSEUS_AGENT_BASE_BRANCH", "dev") == "release"


def test_legacy_env_is_used_until_setting_is_saved(tmp_path, monkeypatch):
    from src.settings import get_setting_or_env

    _saved_settings(tmp_path, monkeypatch, {})
    monkeypatch.setenv("ODYSSEUS_AGENT_BASE_BRANCH", "main")

    assert get_setting_or_env("agent_base_branch", "ODYSSEUS_AGENT_BASE_BRANCH", "dev") == "main"


def test_rag_settings_precede_legacy_env(tmp_path, monkeypatch):
    import src.rag_ranking as ranking

    _saved_settings(tmp_path, monkeypatch, {"rag_tag_credit": 0.25})
    monkeypatch.setenv("ODYSSEUS_RAG_TAG_CREDIT", "0.9")

    assert ranking.tag_credit_scale() == 0.25


def test_vault_date_order_reads_saved_setting(tmp_path, monkeypatch):
    import src.vault_markdown as markdown

    _saved_settings(tmp_path, monkeypatch, {"vault_date_order": "month"})
    monkeypatch.setenv("ODYSSEUS_VAULT_DATE_ORDER", "day")

    assert markdown._date_order() == "month"


def test_upload_limit_reader_reads_saved_setting(tmp_path, monkeypatch):
    import src.upload_limits as limits

    _saved_settings(tmp_path, monkeypatch, {"gallery_upload_max_bytes": 12345})
    monkeypatch.setenv("ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES", "99999")

    assert limits._read_setting_byte_limit(
        "gallery_upload_max_bytes", "ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES", 100
    ) == 12345


def test_migrated_choices_have_schema_controls():
    from src import settings_schema

    for key in (
        "agent_base_branch",
        "chat_upload_max_bytes",
        "rag_recency_halflife_days",
        "stt_beam_size",
        "vault_scan_seconds",
    ):
        spec = settings_schema.get_spec(key)
        assert spec is not None
        assert spec.env_override
