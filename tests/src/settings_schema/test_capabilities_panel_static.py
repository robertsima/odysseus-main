"""The settings schema declares which controls Settings > Advanced draws.

The panel itself is tested in tests/static/js/capabilitiesPanel/.
"""


def test_schema_declares_dynamic_and_suggested_controls():
    from src import settings_schema

    assert settings_schema.get_spec("default_endpoint_id").options_source == "endpoints"
    assert settings_schema.get_spec("default_model").options_source == "models"
    assert settings_schema.get_spec("tts_provider").options_source == "tts_providers"
    assert "alloy" in settings_schema.get_spec("tts_voice").suggestions
    assert settings_schema.get_spec("tts_speed").type == "choice"
