"""The Codex and Claude Agent connection forms in Settings (static/js/settings.js):
the permissions a new API token is offered, and what Save sends.

A token is minted with the ``chat`` scope; its other scopes are granted in a
second step from the toggles on screen. Reading private vault directories
sends their content to a hosted provider, so that one is never on by default.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import expect

pytestmark = pytest.mark.browser

PRIVATE = '.uf-codex-scope[data-scope="vault:read_private"]'
PUBLIC = '.uf-codex-scope[data-scope="vault:read"]'


@pytest.mark.parametrize("kind, label", [("codex", "Codex Agent"), ("claude", "Claude Agent")])
def test_a_new_agent_token_offers_the_vault_scopes_with_private_reads_off(open_app, kind, label):
    page = open_app(1440, chat=False)
    patches = []

    def tokens(route):
        request = route.request
        if request.method == "GET":
            route.fulfill(json=[])
        elif request.method == "PATCH":
            patches.append(json.loads(request.post_data or "{}"))
            route.fulfill(json={"ok": True})
        else:
            route.fulfill(json={"id": "tok-1", "token": "ody_test_token", "name": label})

    page.route("**/api/tokens**", tokens)
    page.click("#user-bar-settings")
    page.click('[data-settings-tab="integrations"]')
    page.click("#unified-intg-add-btn")
    page.click(f'.uf-type-option[data-value="{kind}"]')
    page.click("#uf-codex-create-btn")
    private, public = page.locator(PRIVATE), page.locator(PUBLIC)
    # Both vault scopes can be granted from this form; only the shared one starts on.
    expect(private).to_have_count(1)
    expect(public).to_have_count(1)
    expect(public).to_be_checked()
    expect(private).not_to_be_checked()
    page.click("#uf-codex-save")
    page.wait_for_function("document.getElementById('uf-codex-msg')?.textContent.trim() !== ''")
    granted = [scope for body in patches for scope in body.get("scopes", [])]
    assert "vault:read" in granted and "vault:read_private" not in granted, patches
