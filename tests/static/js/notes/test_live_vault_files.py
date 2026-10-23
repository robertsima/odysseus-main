"""PDF, Word and image files in the Notes vault view, against the real backend."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers.static_app import expect

pytestmark = [pytest.mark.browser, pytest.mark.xdist_group("live_app")]

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
LONG = "A very long agreement name for a client with an extremely long legal entity title v12 final signed.docx"


def _seed(live_app):
    """Write the files next to a Markdown note, in the vault the app reads."""
    with live_app.client() as client:
        resp = client.post("/api/personal/vault/file", json={"path": "vault-files-probe.md", "content": "# note\n"})
        assert resp.status_code in (200, 409), resp.text  # 409: an earlier test made it
    root = next(live_app.data.rglob("vault-files-probe.md")).parent
    folder = root / "Projects" / "Acme" / "Agreements"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "msa.pdf").write_bytes(PDF)
    (folder / "msa.docx").write_bytes(b"PK\x03\x04docx")
    (folder / LONG).write_bytes(b"PK\x03\x04docx")
    (folder / "~$msa.docx").write_bytes(b"lock")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return root


def _open_vault(page):
    page.wait_for_function("!!window.notesModule")
    page.evaluate("window.notesModule.openPanel()")
    page.click('[data-notes-mode="vault"]')
    page.wait_for_selector("[data-vault-file]")
    page.evaluate("document.querySelectorAll('[data-vault-folder]').forEach(d => d.open = true)")


def test_pdf_entry_previews_without_opening_the_editor(live_app, live_page):
    _seed(live_app)
    page = live_page(1440)
    _open_vault(page)
    assert page.locator('[data-vault-asset$="~$msa.docx"]').count() == 0
    pdf = page.locator('[data-vault-asset="Projects/Acme/Agreements/msa.pdf"]')
    expect(pdf).to_contain_text("PDF")
    with page.context.expect_page() as popup:
        pdf.click()
    tab = popup.value
    assert "/api/personal/vault/raw" in tab.url
    assert page.locator("#vault-file-editor").count() == 0
    tab.close()
    with page.expect_download() as dl:
        page.click('[data-vault-download="Projects/Acme/Agreements/msa.docx"]')
    assert dl.value.suggested_filename == "msa.docx"


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1440, "dark"), (390, "light"), (390, "dark")])
def test_vault_list_fits_the_screen(live_app, live_page, width, theme):
    _seed(live_app)
    page = live_page(width)
    colors = {"light": {"bg": "#f0ebe3", "fg": "#5a5248", "panel": "#faf6f0", "border": "#d4cdc2", "red": "#c47d5a"},
              "dark": {"bg": "#111417", "fg": "#f7f8fa", "panel": "#1b2127", "border": "#59636d", "red": "#d7b35a"}}[theme]
    # theme.save also writes the server-side preference, which a later page
    # load would otherwise restore over the local one.
    page.evaluate("([n, c]) => import('/static/js/theme.js').then(m => m.save(n, c))", [theme, colors])
    page.wait_for_function("t => document.documentElement.getAttribute('data-theme') === t", arg=theme)
    page.reload()
    page.wait_for_function("t => document.documentElement.getAttribute('data-theme') === t", arg=theme)
    _open_vault(page)
    page.wait_for_timeout(2500)  # the pane slides in
    pane = page.locator("#notes-pane")
    expect(pane).to_be_visible()
    overflow = page.evaluate(
        "() => { const b = document.querySelector('#notes-pane .notes-pane-body');"
        " return [document.documentElement.scrollWidth - innerWidth, b.scrollWidth - b.clientWidth]; }"
    )
    assert overflow[0] <= 0 and overflow[1] <= 0, overflow
    shots = os.environ.get("ODY_VAULT_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(shots) / f"vault-files-{width}-{theme}.png"))
