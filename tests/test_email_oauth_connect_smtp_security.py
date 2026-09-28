"""Regression coverage for SMTP security saved before Google OAuth."""

from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]


def test_email_tab_oauth_connect_persists_selected_smtp_security():
    # The account editor lives under Settings > Connections (the "uf-" form);
    # the old Settings > Email form ("eaf-") had no markup and was removed.
    # Its "Connect with Google" button saves the account first with the same
    # body the Save button uses, so the chosen SMTP security survives OAuth.
    source = (_REPO / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "el('eaf-oauth-btn')" not in source

    collect_start = source.index("const _collectBody = ")
    collect_body = source[collect_start:source.index("};", collect_start)]
    assert "smtp_security: el('uf-smtp-security').value" in collect_body
    assert "display_name: el('uf-display-name').value.trim()" in collect_body

    start = source.index("el('uf-oauth-btn').addEventListener")
    handler_body = source[start:source.index("window.location.href", start)]
    assert "const body = _collectBody();" in handler_body
