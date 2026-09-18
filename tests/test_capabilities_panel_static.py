"""Regressions for Settings > Capabilities and schema-driven controls."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PANEL = (ROOT / "static/js/capabilitiesPanel.js").read_text(encoding="utf-8")


def test_capabilities_are_collapsed_by_default_and_preserve_user_toggle():
    assert "capabilitiesOpen: false" in PANEL
    assert "state.capabilitiesOpen ? ' open' : ''" in PANEL
    assert "attentionCaps ? ' open' : ''" not in PANEL
    assert "state.capabilitiesOpen = e.target.open" in PANEL


def test_advanced_button_changes_visible_controls_and_confirms_the_change():
    assert "state.showAdvanced = !state.showAdvanced" in PANEL
    assert "Advanced settings are now visible" in PANEL
    assert "some((setting) => setting.advanced)" in PANEL


def test_finite_and_installed_values_use_selects_not_free_text():
    assert "function sourcedOptions" in PANEL
    assert "s.options_source === 'endpoints'" in PANEL
    assert "s.options_source === 'models'" in PANEL
    assert "s.options_source === 'tts_providers'" in PANEL
    assert "s.options_source === 'stt_providers'" in PANEL
    assert "<select id=\"${id}\" class=\"set-input\"" in PANEL
    assert "<datalist id=\"${id}-suggestions\">" in PANEL


def test_schema_declares_dynamic_and_suggested_controls():
    from src import settings_schema

    assert settings_schema.get_spec("default_endpoint_id").options_source == "endpoints"
    assert settings_schema.get_spec("default_model").options_source == "models"
    assert settings_schema.get_spec("tts_provider").options_source == "tts_providers"
    assert "alloy" in settings_schema.get_spec("tts_voice").suggestions
    assert settings_schema.get_spec("tts_speed").type == "choice"
