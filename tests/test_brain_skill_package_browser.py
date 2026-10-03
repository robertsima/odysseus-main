"""Browser-level authoring/navigation in Brain's multi-file skill package UI."""
import os
from pathlib import Path

import pytest
from tests.helpers import agamemnon_browser as fixture
from tests.helpers.agamemnon_browser import Chromium, StaticAppServer, chromium_path, SESSION_ID

pytestmark = pytest.mark.skipif(not chromium_path(), reason='needs Chromium')


@pytest.mark.parametrize('width,height', [(1280, 800), (390, 844)])
def test_package_links_and_resource_editor_render(width, height, monkeypatch):
    original = fixture._api_response
    files = {'references/guide.md': 'A reference', 'scripts/check.py': 'print(1)'}
    state = {'skills': [{'name': 'my-skill', 'description': 'A package', 'status': 'draft', 'source': 'user', 'owner': 'tester',
                         'related_skills': [], 'related_scripts': []},
                        {'name': 'other-skill', 'description': 'Related', 'status': 'draft', 'source': 'user', 'owner': 'tester'}]}
    def response(app_state, method, path, body):
        if path == '/api/skills': return 200, state
        if path == '/api/skills/catalog': return 200, {'skills': []}
        if path == '/api/skills/my-skill/markdown':
            return 200, {'markdown': '---\nname: my-skill\n---\n\n## Procedure\n\n1. Read the guide', 'version': 'v1'}
        if path == '/api/skills/other-skill/markdown':
            return 200, {'markdown': '---\nname: other-skill\n---\n', 'version': 'v1'}
        if path == '/api/skills/my-skill/package':
            return 200, {'files': [{'path': p, 'bytes': len(v)} for p, v in files.items()]}
        if path == '/api/skills/other-skill/package': return 200, {'files': []}
        if path == '/api/skills/my-skill/links' and method == 'PUT':
            state['skills'][0].update(related_skills=body['related_skills'], related_scripts=body['related_scripts'])
            return 200, {'related_skills': body['related_skills'], 'related_scripts': body['related_scripts'],
                         'markdown': '---\nname: my-skill\nrelated_skills: [other-skill]\n---\n', 'version': 'v2'}
        if path == '/api/skills/my-skill/package/file':
            if method == 'GET': return 200, {'path': 'references/guide.md', 'content': files['references/guide.md'], 'version': 'f1'}
            if method == 'PUT':
                files['references/guide.md'] = body['content']
                return 200, {'path': 'references/guide.md', 'content': body['content'], 'version': 'f2'}
        return original(app_state, method, path, body)
    monkeypatch.setattr(fixture, '_api_response', response)
    with StaticAppServer() as server:
        browser = Chromium(chromium_path())
        page = browser.page(width, height)
        try:
            page.goto(f'{server.url}/#{SESSION_ID}', settle=1)
            page.eval('document.getElementById("tool-memory-btn").click()')
            page.eval('document.querySelector(".memory-tab[data-memory-tab=skills]").click()')
            page.wait_for('document.querySelector(".skill-card[data-skill-name=my-skill]")')
            for _ in range(4):
                page.eval('if (!document.querySelector(".skill-card[data-skill-name=my-skill]").classList.contains("doclib-card-expanded")) document.querySelector(".skill-card[data-skill-name=my-skill]").click()')
                try:
                    page.wait_for('document.querySelector(".skill-card[data-skill-name=my-skill].doclib-card-expanded .skill-link-row select option[value=other-skill]")', timeout=2)
                    break
                except fixture.BrowserError:
                    continue
            else: pytest.fail('Skill package did not load: ' + str(page.eval('document.querySelector(".skill-package")?.textContent')))
            for _ in range(5):
                page.eval('''(() => { const select = document.querySelector('.skill-card[data-skill-name=my-skill].doclib-card-expanded .skill-link-row select');
                    if (!select?.querySelector('option[value=other-skill]')) return;
                    select.value = 'other-skill'; select.dispatchEvent(new Event('change')); })()''')
                try:
                    page.wait_for('document.querySelector(".skill-card[data-skill-name=my-skill].doclib-card-expanded .skill-link-row .skill-package-file")', timeout=2)
                    break
                except fixture.BrowserError:
                    page.eval('if (!document.querySelector(".skill-card[data-skill-name=my-skill]").classList.contains("doclib-card-expanded")) document.querySelector(".skill-card[data-skill-name=my-skill]").click()')
            else: pytest.fail('Related skill link did not render')
            assert page.eval('document.querySelector(".skill-link-row .skill-package-file").textContent === "other-skill"')
            page.eval('''(() => { const select = document.querySelectorAll('.skill-link-row select')[1];
                select.value = 'scripts/check.py'; select.dispatchEvent(new Event('change')); })()''')
            page.wait_for('document.querySelectorAll(".skill-link-row .skill-package-file").length === 2')
            capture = Path(os.environ.get('VISUAL_CHECK_DIR', '.visual-check'))
            capture.mkdir(parents=True, exist_ok=True)
            page.eval('document.querySelector(".skill-package-links").scrollIntoView({block:"nearest"})')
            page.screenshot(capture / f'brain-skill-links-{width}.png')
            page.eval('document.querySelector(".skill-link-row .skill-package-file").click()')
            page.wait_for('document.querySelector(".skill-card[data-skill-name=other-skill].doclib-card-expanded")')
            page.eval('document.querySelector(".skill-card[data-skill-name=my-skill]").click()')
            page.wait_for('document.querySelector(".skill-card[data-skill-name=my-skill].doclib-card-expanded")')
            page.eval('document.querySelector(".skill-package-list .skill-package-file").click()')
            page.wait_for('document.querySelector(".skill-package-text")')
            assert page.eval('document.querySelector(".skill-package-text").value === "A reference"')
            layout = page.eval('''(() => { const card = document.querySelector('.skill-card.doclib-card-expanded');
              const save = [...card.querySelectorAll('.skill-package-editor button')].find(b => b.textContent === 'Save file');
              const rect = save.getBoundingClientRect(); return {bottom: rect.bottom, viewport: innerHeight,
              horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 2}; })()''')
            assert not layout['horizontalOverflow'], layout
            if width < 768: assert layout['bottom'] <= height + 1, layout
            page.screenshot(capture / f'brain-skill-editor-{width}.png')
        finally:
            page.close(); browser.close()
