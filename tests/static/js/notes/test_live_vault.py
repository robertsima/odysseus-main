"""Vault files in the Notes panel (static/js/notes.js) against the real backend."""
from __future__ import annotations

import re

import pytest

from tests.helpers.static_app import expect

pytestmark = [pytest.mark.browser, pytest.mark.xdist_group("live_app")]

NOTE = "browser-probe.md"


def test_vault_note_opens_edits_and_saves(live_app, live_page):
    with live_app.client() as client:
        resp = client.post("/api/personal/vault/file",
                           json={"path": NOTE, "content": "# Browser probe\n\nVAULT-PROBE-CONTENT\n"})
        assert resp.status_code == 200, resp.text
    page = live_page(1440)
    page.wait_for_function("!!window.notesModule")
    page.evaluate("window.notesModule.openPanel()")
    page.click('[data-notes-mode="vault"]')
    page.click(f'[data-vault-file="{NOTE}"]')
    editor = page.locator("#vault-file-editor")
    expect(editor).to_have_value(re.compile("VAULT-PROBE-CONTENT"))

    save = page.locator("#vault-file-save")
    expect(save).to_be_disabled()  # nothing to save yet
    editor.fill(editor.input_value() + "EDITED-IN-BROWSER\n")
    expect(save).to_be_enabled()
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/personal/vault/file" in r.url) as saved:
        save.click()
    assert saved.value.ok, saved.value.text()
    expect(save).to_be_disabled()

    with live_app.client() as client:
        stored = client.get("/api/personal/vault/file", params={"path": NOTE}).json()
    assert "VAULT-PROBE-CONTENT" in stored["content"] and "EDITED-IN-BROWSER" in stored["content"], stored
