"""Authoring and navigating a multi-file skill package in Brain
(static/js/skills.js): related-skill and script links, the resource editor and
the SKILL.md editor with its unsaved draft.

Ported from the CDP-driven tests/test_brain_skill_package_browser.py.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from tests.helpers.static_app import expect

pytestmark = pytest.mark.browser

CARD = '.skill-card[data-skill-name="my-skill"]'
OTHER = '.skill-card[data-skill-name="other-skill"]'


class SkillsApi:
    """The skills endpoints Brain talks to, with one package and one sibling skill."""

    def __init__(self, page):
        self.requests: list[tuple[str, str]] = []
        self.files = {"references/guide.md": "A reference", "scripts/check.py": "print(1)"}
        self.skills = [
            {"name": "my-skill", "description": "A package", "status": "draft", "source": "user", "owner": "tester",
             "related_skills": [], "related_scripts": []},
            {"name": "other-skill", "description": "Related", "status": "draft", "source": "user", "owner": "tester"},
        ]
        page.route("**/api/skills**", self._serve)

    def _serve(self, route):
        request = route.request
        path, method = urlsplit(request.url).path, request.method
        body = json.loads(request.post_data or "{}") if method in ("POST", "PUT", "PATCH") else {}
        self.requests.append((method, path))
        answer = self._answer(method, path, body)
        if answer is None:
            route.fallback()
        else:
            status, payload = answer
            route.fulfill(status=status, json=payload)

    def _answer(self, method, path, body):
        if path == "/api/skills":
            return 200, {"skills": self.skills}
        if path == "/api/skills/catalog":
            return 200, {"skills": []}
        if path == "/api/skills/my-skill/markdown" and method == "POST":
            return 409, {"detail": "Conflict: skill changed on disk"}
        if path == "/api/skills/my-skill/markdown":
            return 200, {"markdown": "---\nname: my-skill\n---\n\n## Procedure\n\n1. Read the guide", "version": "v1"}
        if path == "/api/skills/other-skill/markdown":
            return 200, {"markdown": "---\nname: other-skill\n---\n", "version": "v1"}
        if path == "/api/skills/my-skill/package":
            return 200, {"files": [{"path": p, "bytes": len(v)} for p, v in self.files.items()]}
        if path == "/api/skills/other-skill/package":
            return 200, {"files": []}
        if path == "/api/skills/my-skill/links" and method == "PUT":
            self.skills[0].update(related_skills=body["related_skills"], related_scripts=body["related_scripts"])
            return 200, {"related_skills": body["related_skills"], "related_scripts": body["related_scripts"],
                         "markdown": "---\nname: my-skill\nrelated_skills: [other-skill]\n---\n", "version": "v2"}
        if path == "/api/skills/my-skill/package/file":
            if method == "GET":
                return 200, {"path": "references/guide.md", "content": self.files["references/guide.md"], "version": "f1"}
            if method == "PUT":
                self.files["references/guide.md"] = body["content"]
                return 200, {"path": "references/guide.md", "content": body["content"], "version": "f2"}
        return None


def open_skill(page):
    """Open Brain's skills tab and expand my-skill. The list renders in two
    passes (cards, then each package), and a pass can collapse the card, so
    expand again until the package's link picker is there."""
    page.evaluate("document.getElementById('tool-memory-btn').click()")
    page.evaluate("document.querySelector('.memory-tab[data-memory-tab=skills]').click()")
    page.wait_for_selector(CARD)
    picker = f"{CARD}.doclib-card-expanded .skill-link-row select option[value=other-skill]"
    for _ in range(5):
        page.evaluate("(sel) => { const c = document.querySelector(sel);"
                      " if (!c.classList.contains('doclib-card-expanded')) c.querySelector('.skill-card-toggle').click(); }", CARD)
        if page.locator(picker).count():
            return
        page.wait_for_timeout(500)
    raise AssertionError("the skill package did not load")


@pytest.mark.parametrize("width, height", [(1280, 800), (390, 844)])
def test_package_links_and_the_resource_editor_work_and_stay_on_screen(open_app, width, height):
    page = open_app(width, height=height, chat=False)
    api = SkillsApi(page)
    open_skill(page)
    related = page.locator(f"{CARD} .skill-link-row").first
    related.locator("select").select_option("other-skill")
    link = page.locator(f"{CARD} .skill-link-row .skill-package-file")
    expect(link).to_have_text("other-skill")
    expect(page.locator(f"{CARD} .skill-link-remove")).to_have_attribute("aria-label", "Unlink skill other-skill")
    page.locator(f"{CARD} .skill-link-row select").nth(1).select_option("scripts/check.py")
    expect(page.locator(f"{CARD} .skill-link-row .skill-package-file")).to_have_count(2)
    assert ("PUT", "/api/skills/my-skill/links") in api.requests
    assert not [1 for method, path in api.requests if method == "POST" and ("/test" in path or "/run" in path)], \
        "linking a script must not run it"

    # Following a link opens that skill and leaves keyboard focus on its header.
    link.first.click()
    expect(page.locator(f"{OTHER}.doclib-card-expanded")).to_have_count(1)
    page.wait_for_function("(sel) => document.activeElement === document.querySelector(sel)", arg=f"{OTHER} .skill-card-toggle")
    page.locator(f"{CARD} .skill-card-toggle").evaluate("el => el.click()")
    expect(page.locator(f"{CARD}.doclib-card-expanded")).to_have_count(1)

    page.locator(".skill-package-list .skill-package-file").first.click()
    editor = page.locator(".skill-package-text")
    expect(editor).to_have_value("A reference")
    layout = page.evaluate("""() => {
      const card = document.querySelector('.skill-card.doclib-card-expanded');
      const save = [...card.querySelectorAll('.skill-package-editor button')].find(b => b.textContent === 'Save file');
      return {bottom: save.getBoundingClientRect().bottom, sideways: document.documentElement.scrollWidth > innerWidth + 2};
    }""")
    assert not layout["sideways"], layout
    if width < 768:
        assert layout["bottom"] <= height + 1, layout


def test_the_skill_header_toggles_from_the_keyboard_and_an_unsaved_skill_md_draft_survives_a_failed_save(open_app):
    page = open_app(1280, height=800, chat=False)
    SkillsApi(page)
    open_skill(page)
    toggle = page.locator(f"{CARD} .skill-card-toggle")
    toggle.focus()
    page.keyboard.press("Space")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.focus()
    page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")

    page.locator(f"{CARD} .doclib-card-action-btn:nth-child(2)").click()
    editor = page.locator(".skill-md-editor")
    expect(editor).to_have_count(1)
    assert page.evaluate("document.querySelector('.skill-md-editor').labels.length") > 0
    editor.evaluate("el => { el.value += '\\nDraft stays'; el.dispatchEvent(new Event('input', {bubbles: true})); }")
    save = page.locator(".skill-md-save")
    save.click()
    expect(save).to_be_enabled()
    # The save was refused (409): the text typed is still there, not reset.
    assert "Draft stays" in editor.input_value()
    # Cancelling asks first; declining keeps the editor, and closing Brain does not discard it.
    page.evaluate("window.confirm = () => false")
    page.locator(".skill-md-cancel").click()
    expect(editor).to_have_count(1)
    page.evaluate("document.getElementById('close-memory-modal').click()")
    assert page.evaluate("!document.getElementById('memory-modal').classList.contains('hidden')")
    page.evaluate("window.confirm = () => true")
    page.locator(".skill-md-cancel").click()
    expect(editor).to_have_count(0)
