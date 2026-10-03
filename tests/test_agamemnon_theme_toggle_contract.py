from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THEME = (ROOT / "static/js/theme.js").read_text()
HTML = (ROOT / "static/index.html").read_text()


def test_builtin_visual_theme_options_and_legacy_identifier():
    assert "dark:       { bg:'#111417', fg:'#f7f8fa'" in THEME
    assert "odysseus:   { bg:'#211f1c'" in THEME
    assert "const DEFAULT_THEME = 'dark'" in THEME
    assert "const LS_KEY = 'odysseus-theme'" in THEME
    assert "const THEME_PREF = 'theme'" in THEME


def test_builtins_have_distinct_colorway_labels_without_changing_brand():
    assert "name === 'dark' ? 'Obsidian'" in THEME
    assert "name === 'odysseus' ? 'Bronze'" in THEME
    assert "Object.entries(THEMES).map(([name, c])" in THEME


def test_legacy_dark_preference_is_not_migrated_or_overwritten():
    assert "const LS_KEY = 'odysseus-theme'" in THEME
    assert "const THEME_PREF = 'theme'" in THEME
    assert "if (saved && saved.colors)" in THEME
    assert "const currentColors = saved ? saved.colors : THEMES[DEFAULT_THEME]" in THEME
    assert "if (!t || !t.colors)" in HTML
    assert "if (t && t.colors)" in HTML


def test_theme_font_is_applied_independently_of_palette():
    assert "function applyFontDensity" in THEME
    assert "const DEFAULT_FONT = 'mono'" in THEME
    assert "font: opts.font" in THEME or "opts.font" in THEME
    assert "odysseus-ui-scale" in HTML


def test_first_paint_agamemnon_defaults_include_palette_tokens():
    for token in ('--bg', '--fg', '--panel', '--border', '--red', '--brand-color'):
        assert f"ds.setProperty('{token}'" in HTML
    assert "ds.setProperty('--red', '#f0c45a')" in HTML
    assert "ds.setProperty('--brand-color', '#f0c45a')" in HTML


def test_favicon_identity_is_not_recolored_by_theme():
    assert "_updateFavicon(colors.red" not in THEME
    assert "var ac = c.red" not in HTML
    assert "function _updateFavicon" in THEME  # retained definition has no theme caller
    assert "_updateFavicon(" not in THEME.replace("function _updateFavicon(", "")


def test_gallery_retains_custom_themes_and_named_controls():
    assert "_loadCustomThemes()" in THEME
    assert "saveCustomTheme" in THEME
    assert "applyFontDensity" in THEME
    assert "applyBgPattern" in THEME
    assert "applyFrostedGlass" in THEME
    assert "theme-swatch-name" in THEME


def test_current_accessibility_bootstrap_remains_present():
    assert "odysseus-ui-scale" in HTML
    assert "odysseus-theme" in HTML
    assert "localStorage.getItem('odysseus-theme')" in HTML

# Additional checks above complement the existing broad theme contracts.

def test_first_paint_uses_default_only_when_no_saved_palette():
    assert "if (!t || !t.colors)" in HTML
    assert "ds.setProperty('--bg', '#111417')" in HTML
    assert "if (t && t.colors)" in HTML


def test_theme_switch_does_not_mutate_identity_assets():
    assert "_updateFavicon(colors.red" not in THEME
    # First-paint route favicon logic is kept as-is; no theme-color favicon
    # rewrite should remain in the early palette application.
    assert "var ac = c.red" not in HTML


def test_custom_theme_and_accessibility_controls_remain_supported():
    assert "saveCustomTheme" in THEME and "_loadCustomThemes" in THEME
    assert "applyFontDensity" in THEME
    assert "applyBgPattern" in THEME
    assert "applyFrostedGlass" in THEME
    assert "odysseus-ui-scale" in HTML
