"""Agamemnon gets a distinct navigation/layout composition, not just a
palette; Odysseus's original geometry, and the permanent brand/agent
identity assets, must survive untouched. See
tests/test_agamemnon_theme_toggle_contract.py for the earlier palette-only
contract this slice builds on.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THEME = (ROOT / "static/js/theme.js").read_text()
HTML = (ROOT / "static/index.html").read_text()
LOGIN = (ROOT / "static/login.html").read_text()
STYLE = (ROOT / "static/style.css").read_text()
MOCKUP_STYLE = (ROOT / "static/agamemnon-mockup.css").read_text()

HELMET_DOME = "M6 18.5C6 10.7 10.3 5 16 5s10 5.7 10 13.5"
HELMET_CREST = "M16 3c-2.4 1.9-3.7 3.4-3.7 5 0 1 .6 1.8 1.6 2l2.1.4 2.1-.4c1-.2 1.6-1 1.6-2 0-1.6-1.3-3.1-3.7-5Z"
BRAND_CREST = (ROOT / "static/branding/agamemnon-trojan-helmet.svg").read_text()


def test_explicit_root_theme_identity_attribute_exists_and_defaults_to_agamemnon():
    assert "export function applyThemeIdentity(name)" in THEME
    assert "document.documentElement.setAttribute('data-theme', name || DEFAULT_THEME)" in THEME
    # First paint sets it before any module loads, defaulting to Agamemnon
    # (DEFAULT_THEME) so the layout never flashes the wrong composition.
    assert "document.documentElement.setAttribute('data-theme', 'dark');" in HTML
    assert "if (t && t.name) document.documentElement.setAttribute('data-theme', t.name);" in HTML


def test_theme_identity_is_synced_on_every_commit_path_not_just_swatch_clicks():
    # save() is the single choke point every theme-apply flow (swatch click,
    # /theme slash command, cross-tab socket sync, custom theme save) already
    # calls — hooking it there covers all of them without touching each
    # call site individually.
    save_fn = THEME.split("export function save(name, colors, opts) {", 1)[1].split("\nexport function", 1)[0]
    assert "applyThemeIdentity(name);" in save_fn
    # The two flows that apply colors without going through save() (initial
    # boot sync, and the "reset to default" button) set it explicitly too.
    assert "applyThemeIdentity(saved ? saved.name : DEFAULT_THEME);" in THEME
    assert "applyThemeIdentity(DEFAULT_THEME);" in THEME


def test_agamemnon_layout_is_scoped_to_the_identity_attribute_not_a_palette_color():
    block = STYLE.split("Agamemnon layout", 1)[1].split("Fixed hamburger", 1)[0]
    assert 'html[data-theme="dark"] .sidebar .sidebar-header' in block
    assert 'html[data-theme="dark"] .sidebar .sidebar-brand' in block
    assert 'html[data-theme="dark"] .icon-rail #rail-gallery' in block
    assert 'html[data-theme="dark"] .icon-rail #rail-agents' in block
    for selector_line in ("flex-direction: column;", "margin-top: 11px;"):
        assert selector_line in block


def test_odysseus_original_sidebar_geometry_is_unchanged():
    # The pre-existing, theme-agnostic rules every other theme (including
    # Odysseus itself) renders with must still be exactly as they were.
    assert "    .sidebar-header {\n      display: flex;\n      align-items: center;\n      justify-content: flex-end; /* right-align when sidebar is on left */" in STYLE
    assert "    .sidebar-brand {\n      display: flex;\n      align-items: center;\n      flex-shrink: 0;\n      min-height: 24px;\n    }" in STYLE
    assert "    .sidebar-brand-title {\n      font-size: 1rem;" in STYLE
    assert ".icon-rail {\n      width: 48px;" in STYLE


def test_permanent_helmet_logo_is_unconditional_across_every_palette():
    # The brand mark lives directly in the static markup (not behind a
    # theme-name check), so switching palettes recolors it via currentColor
    # but can never swap it for a different icon.
    assert "brand-crest-icon" in STYLE
    assert ".brand-crest-icon {" in STYLE
    assert "color: var(--brand-color, var(--red));" in STYLE.split(".brand-crest-icon {", 1)[1][:200]

    for doc in (HTML, LOGIN):
        # Static markup and favicon use the shared crest asset; inline brand
        # marks remain Agamemnon helmet vectors, never headphone/mic imagery.
        if doc is HTML:
            assert '/static/branding/agamemnon-trojan-helmet.svg' in doc
            assert 'href="/static/icons/icon-192.png"' in doc
        assert 'stroke="currentColor"' not in doc or 'brand-crest-icon' in doc
    assert "side-profile Trojan helmet" in BRAND_CREST and '#d7b35a' in BRAND_CREST
    assert 'game-icons:spartan-helmet by Delapouite' in BRAND_CREST
    assert HELMET_DOME not in HTML and HELMET_DOME not in LOGIN
    assert "headphone" not in HTML.lower() and "headphone" not in LOGIN.lower()

    # Per-route favicons use the exact crest shape, rather than route glyphs
    # or the former headset silhouette.
    assert HELMET_DOME not in HTML.split("var SHAPES", 1)[1].split("var inner", 1)[0]
    assert "icons: [" in HTML and "'/static/icons/icon-192.png'" in HTML
    assert "var isAgamemnon = !theme || !theme.name || theme.name === 'dark'" in HTML
    assert "agFav.href = '/static/branding/agamemnon-trojan-helmet.svg'" in HTML
    assert "agApple.href = '/static/branding/agamemnon-trojan-helmet.svg'" in HTML
    assert "name:'Agamemnon',short_name:'Agamemnon'" in HTML
    assert "'/calendar': 'Calendar — Odysseus'" in HTML
    assert "name: (titles[path] || 'Odysseus')" in HTML
    assert "document.title = titles[path] || 'Odysseus'" in HTML
    assert HELMET_DOME in THEME
    assert "M16 4L16 22L6 22Z" not in THEME
    assert "M16 4L16 22L6 22Z" not in HTML
    assert "M16 4L16 22L6 22Z" not in LOGIN


def test_agamemnon_agents_use_distinct_model_colored_soldier_artworks():
    dashboard = (ROOT / "static/js/agentsDashboard.js").read_text()
    marks = (ROOT / "static/branding/agamemnon-agent-marks.svg").read_text()
    for variant in ("primary", "worker", "scout", "reviewer", "specialist"):
        assert f'id="soldier-{variant}"' in marks
    assert 'fill="currentColor"' in marks
    assert 'href="#soldier-${soldierVariant}"' in dashboard
    assert '<!-- AGAMEMNON SOLDIER SYMBOLS START (generated) -->' in HTML
    assert 'data-soldier-variant="${soldierVariant}"' in dashboard
    identity = (ROOT / "static/js/agamemnonIdentity.js").read_text()
    assert "resolveAgamemnonModelIdentity" in dashboard
    assert "matchIdentity(model) || matchIdentity(source)" in identity
    for family in ("anthropic", "openai", "google", "mistral", "local", "default"):
        assert f"'{family}'" in identity
    assert "--agent-model-color:${modelIdentity.color}" in dashboard
    assert 'data-model-family="${modelIdentity.family}"' in dashboard
    # The legacy role symbols remain available only for the unchanged
    # Odysseus presentation; Agamemnon CSS selects the generic Trojan mark.
    assert 'html[data-theme="dark"] .ag-seal-mark{display:none}' in MOCKUP_STYLE
    assert 'html[data-theme="dark"] .ag-soldier-sprite{display:block' in MOCKUP_STYLE
    assert 'id="soldier-primary"' in HTML
    assert 'id="command"' in HTML
    assert 'ag-chat-agent-mark' not in HTML
    assert 'ag-run-agent-mark' not in HTML
    assert '"name": "Odysseus"' in (ROOT / "static/manifest.json").read_text()
    assert "icons/icon-192.png" in (ROOT / "static/manifest.json").read_text()
    assert '"background_color": "#111417"' in (ROOT / "static/manifest.json").read_text()
    assert '"theme_color": "#111417"' in (ROOT / "static/manifest.json").read_text()


def test_live_color_edit_fallback_to_custom_slot_preserves_prior_identity():
    # Editing a color picker while a theme is active but not yet saved under
    # its own name (e.g. tweaking Agamemnon's palette directly) auto-saves
    # into the transient 'custom' storage slot. That's a storage-bucket
    # choice, not a theme switch, so — unlike a swatch click or "Save as" —
    # it must not hand `save()`'s internal applyThemeIdentity('custom') call
    # the final word and silently drop Agamemnon's (or any theme's) layout.
    assert "function _saveFullKeepIdentity(name, colors) {" in THEME
    keep_fn = THEME.split("function _saveFullKeepIdentity(name, colors) {", 1)[1].split("\n  }", 1)[0]
    assert "document.documentElement.getAttribute('data-theme')" in keep_fn
    assert "save(name, colors, _getOpts());" in keep_fn
    assert "applyThemeIdentity(_identity);" in keep_fn
    # Both the basic and advanced color-picker live-edit fallbacks, plus the
    # "clear advanced overrides" button, route through the identity-safe
    # helper instead of the plain `_saveFull`, which would clobber identity.
    assert THEME.count("_saveFullKeepIdentity('custom',") == 3


def test_agent_seal_sprites_are_unmodified_by_this_slice():
    # The per-agent "seal" icon (shield + helm) is a separate, already-shipped
    # identity asset (static/js/agentsDashboard.js); this slice must not
    # touch its shape, only the layout around it.
    assert "ag-seal-shield" in STYLE and "ag-seal-helm" in STYLE and "ag-seal-brow" in STYLE
    assert ".ag-seal-helm { fill: color-mix(in srgb, var(--wb-accent) 22%, var(--panel)); stroke: var(--fg);" in STYLE
