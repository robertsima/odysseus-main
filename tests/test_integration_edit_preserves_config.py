"""Editing an integration must not silently destroy what is already stored.

Responses mask the API key, so the edit form opens with a blank key field. If a
blank (or round-tripped mask) overwrote the stored key, saving any other change
— a renamed integration, a flipped enabled toggle — would break the connection.
Switching preset must also re-seed the preset's own defaults, or the assistant
keeps being told about the previous service's endpoints.
"""

import pytest

from src import integrations as integ


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(integ, "DATA_FILE", str(tmp_path / "integrations.json"))
    monkeypatch.setattr(integ, "DATA_DIR", str(tmp_path), raising=False)
    yield


def _make(**over):
    body = {
        "preset": "miniflux",
        "name": "Miniflux",
        "base_url": "http://reader.local",
        "api_key": "secret-token-value",
    }
    body.update(over)
    return integ.add_integration(body)


class TestApiKeyIsNotLostOnEdit:
    def test_renaming_keeps_the_key(self):
        item = _make()
        updated = integ.update_integration(item["id"], {"name": "Reader"})
        assert updated["name"] == "Reader"
        assert updated["api_key"] == "secret-token-value"

    def test_blank_key_means_unchanged(self):
        item = _make()
        updated = integ.update_integration(item["id"], {"api_key": ""})
        assert updated["api_key"] == "secret-token-value"

    def test_masked_key_round_trip_is_ignored(self):
        """A client that saves back exactly what the list endpoint showed it."""
        item = _make()
        masked = integ.mask_integration_secret(item)["api_key"]
        assert masked != "secret-token-value"
        updated = integ.update_integration(item["id"], {"api_key": masked})
        assert updated["api_key"] == "secret-token-value"

    def test_a_real_new_key_still_replaces_it(self):
        item = _make()
        updated = integ.update_integration(item["id"], {"api_key": "rotated"})
        assert updated["api_key"] == "rotated"


class TestEditableFields:
    def test_description_is_writable(self):
        """The description is what the assistant reads to know the endpoints,
        so a user has to be able to correct it."""
        item = _make()
        updated = integ.update_integration(item["id"], {"description": "GET /v1/mine"})
        assert updated["description"] == "GET /v1/mine"
        assert "GET /v1/mine" in integ.get_integrations_prompt()

    def test_disabling_hides_it_from_the_assistant_without_deleting_it(self):
        item = _make()
        integ.update_integration(item["id"], {"enabled": False})
        assert "Miniflux" not in integ.get_integrations_prompt()
        assert integ.get_integration(item["id"]) is not None

    def test_switching_preset_reseeds_that_preset_defaults(self):
        item = _make()
        updated = integ.update_integration(item["id"], {"preset": "linkding"})
        assert updated["preset"] == "linkding"
        assert "bookmark" in updated["description"].lower()
        assert updated["auth_header"] == integ.INTEGRATION_PRESETS["linkding"]["auth_header"]

    def test_submitted_fields_win_over_preset_defaults(self):
        item = _make()
        updated = integ.update_integration(
            item["id"], {"preset": "linkding", "name": "My Bookmarks"}
        )
        assert updated["name"] == "My Bookmarks"

    def test_resaving_the_same_preset_keeps_a_customised_description(self):
        item = _make()
        integ.update_integration(item["id"], {"description": "only /v1/entries please"})
        updated = integ.update_integration(item["id"], {"preset": "miniflux", "name": "Miniflux"})
        assert updated["description"] == "only /v1/entries please"
