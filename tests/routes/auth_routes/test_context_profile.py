"""The Context settings tab's route: /api/auth/settings/context-profile.

static/js/settings.js loads and saves profiles at this path (tested in
tests/static/js/settings/context_profiles.test.mjs). The tab once called a
path nothing served, so every load 404'd.
"""
import pytest

PATH = "/api/auth/settings/context-profile"
TARGET = {"endpoint_url": "http://models.test/v1", "model": "tiny-model"}


def test_any_signed_in_user_can_read_the_profile(api):
    response = api.as_user("alice").get(PATH, params=TARGET)

    assert response.status_code == 200, response.text
    body = response.json()
    assert {"balanced", "compact"} <= set(body["presets"])
    assert body["selected"] == ""


@pytest.mark.security
def test_only_an_admin_can_save_a_profile(api):
    response = api.as_user("alice").post(PATH, json={**TARGET, "preset": "compact"})

    assert response.status_code == 403


def test_a_saved_preset_is_what_the_next_read_reports(api):
    admin = api.as_admin()
    saved = admin.post(PATH, json={**TARGET, "preset": "compact"})
    try:
        assert saved.status_code == 200, saved.text
        assert admin.get(PATH, params=TARGET).json()["selected"] == "compact"
    finally:
        admin.post(PATH, json={**TARGET, "preset": ""})
