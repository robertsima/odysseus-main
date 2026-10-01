from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text()
CSS = (ROOT / "static/agamemnon-mockup.css").read_text()
STYLE = (ROOT / "static/style.css").read_text()
SPEC = (ROOT / "website/agamemnon-visual-difference-spec.md").read_text()


def test_mockup_styles_are_identity_scoped_and_loaded_after_base_styles():
    assert '/static/agamemnon-mockup.css' in HTML
    assert HTML.index('/static/style.css') < HTML.index('/static/agamemnon-mockup.css')
    assert 'html[data-theme="dark"]' in CSS
    assert 'html[data-theme="odysseus"]' not in CSS
    assert '.ag-page-heading,.ag-context-rail' in CSS


def test_exact_penpot_geometry_palette_and_typography_are_present():
    for token in ('240px', '306px', '790px', '#111417', '#171c21', '#1b2127', '#20262c', '#343d45', '#59636d', '#d7b35a', '#e07b5a'):
        assert token in CSS
    assert "font-family:'Space Grotesk'" in CSS
    assert "SpaceGrotesk-Regular.woff2" in STYLE
    assert "SpaceGrotesk-Bold.woff2" in STYLE
    assert (ROOT / 'static/fonts/SpaceGrotesk-Regular.woff2').stat().st_size > 10_000
    assert (ROOT / 'static/fonts/SpaceGrotesk-Bold.woff2').stat().st_size > 10_000


def test_three_mockup_surface_titles_and_context_composition_exist():
    for label in ('CHAT / STRATEGY ROOM', 'AGENT SPACE / PHALANX', 'WORKBENCH / RUN CONTROL'):
        assert label in HTML
    for id_ in ('ag-open-agents', 'ag-open-workbench', 'ag-open-theme'):
        assert id_ in HTML
    assert 'SESSION CONTEXT' in HTML and 'RUN CONTROLS' in HTML and 'EVIDENCE' in HTML


def test_visual_spec_and_static_render_artifacts_cover_every_board():
    assert '## Differences observed before implementation' in SPEC
    assert '## Acceptance criteria' in SPEC
    for page in ('chat', 'agents', 'workbench'):
        preview = ROOT / f'website/agamemnon-preview-{page}.html'
        assert preview.exists() and preview.stat().st_size > 1000
        preview_html = preview.read_text()
        assert 'agamemnon-preview.css' in preview_html
        assert 'class="brand-crest"' in preview_html
        assert 'aria-label="Agamemnon hoplite crest"' in preview_html
        assert '<img' not in preview_html


def test_responsive_fallback_and_odysseus_labels_remain():
    assert '@media(max-width:1050px)' in CSS
    assert '@media(max-width:700px)' in CSS
    assert 'ody-agents-title' in HTML and 'ody-workbench-title' in HTML
    assert 'html:not([data-theme="dark"])' in CSS
