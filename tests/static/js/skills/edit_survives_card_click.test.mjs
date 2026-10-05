// Brain > Skills in static/js/skills.js: a click on a skill card toggles it
// open and closed. While the card's SKILL.md is being edited, a click on the
// card outside the textarea collapsed the card and threw the unsaved edit
// away (#4002). A click that would close the card now asks first, and the
// card stays open while the user answers no.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

fake.route('GET', '/api/skills', () => ({
  skills: [{ name: 'deploy-check', description: 'Check a deploy', status: 'draft', confidence: 0.8, uses: 2 }],
}));
fake.route('GET', '/api/skills/catalog', () => ({ skills: [] }));
fake.route('GET', '/api/skills/deploy-check/markdown', () => ({ markdown: '# deploy-check\nold body' }));

document.body.innerHTML = '<div class="admin-card"><div id="skills-list"></div></div>';

const { loadSkills } = await import('../../../../static/js/skills.js');

test('a click on the card body while editing keeps the card open and the edit', async () => {
  await loadSkills();
  const card = document.querySelector('.skill-card[data-skill-name="deploy-check"]');
  card.querySelector('.skill-card-name').click();
  await waitFor(() => card.querySelector('.skill-md-pre').textContent.includes('old body'),
    { what: 'the SKILL.md to load' });

  const editBtn = [...card.querySelectorAll('.doclib-card-action-btn')].find((b) => b.textContent.includes('Edit'));
  editBtn.click();
  const editor = card.querySelector('textarea.skill-md-editor');
  editor.value = '# deploy-check\nnew body, not saved yet';

  // The padding around the textarea, then the card's title bar.
  card.querySelector('.skill-card-preview').click();
  assert.ok(card.classList.contains('doclib-card-expanded'), 'a click beside the textarea kept the card open');
  const asked = [];
  window.confirm = (message) => { asked.push(message); return false; };
  card.querySelector('.skill-card-name').click();

  assert.deepEqual(asked, ['Discard unsaved SKILL.md changes?']);
  assert.ok(card.classList.contains('doclib-card-expanded'), 'the card stayed open');
  assert.equal(card.querySelector('textarea.skill-md-editor')?.value, '# deploy-check\nnew body, not saved yet');
});
