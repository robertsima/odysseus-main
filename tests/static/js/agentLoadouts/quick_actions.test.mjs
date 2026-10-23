// Loadout cards offer Talk / Assign, and the editor saves quick actions
// (static/js/agentLoadouts.js). A card that is not saved yet has no buttons,
// because the server cannot talk to or assign an agent that does not exist.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => { fake.restore(); await dom.restore(); });

const saved = [];
fake.route('GET', '/api/presets/templates', () => []);
fake.route('GET', '/api/agents/profiles/templates', () => ({ templates: [] }));
fake.route('GET', '/api/auth/settings', () => ({}));
fake.route('POST', '/api/auth/settings', ({ body }) => { const b = JSON.parse(body); saved.push(b.agent_profiles); return b; });

const { mountLoadoutsEditor } = await import('../../../../static/js/agentLoadouts.js');
const host = document.createElement('div');
document.body.appendChild(host);
mountLoadoutsEditor(host, {
  canEdit: true,
  profiles: [{ name: 'Reader', quick_actions: [{ label: 'Summarise', prompt: 'Summarise {document}' }] }],
});

test('a saved loadout card has both task buttons and its quick action chips', () => {
  const card = host.querySelector('.agent-profile-item summary');
  assert.deepEqual([...card.querySelectorAll('[data-agent-act]')].map((b) => b.textContent), ['Talk to agent', 'Assign a task']);
  assert.deepEqual([...card.querySelectorAll('.aa-chip')].map((b) => b.textContent), ['Summarise']);
});

test('a loadout that is not saved yet has no task buttons', () => {
  [...host.querySelectorAll('button')].find((b) => b.textContent === 'Add loadout').click();
  const cards = host.querySelectorAll('.agent-profile-item');
  assert.equal(cards.length, 2);
  assert.equal(cards[1].querySelectorAll('[data-agent-act]').length, 0);
});

test('quick actions are edited in the card, capped at six, and saved with the loadout', async () => {
  const card = host.querySelector('.agent-profile-item');
  const add = [...card.querySelectorAll('button')].find((b) => b.textContent === 'Add quick action');
  for (let i = 0; i < 5; i += 1) add.click();
  assert.equal(card.querySelectorAll('.agent-quick-row').length, 6);
  assert.equal(add.disabled, true, 'a seventh cannot be added');
  const rows = card.querySelectorAll('.agent-quick-row');
  const last = rows[5];
  last.querySelector('input').value = 'List risks';
  last.querySelector('input').dispatchEvent(new window.Event('input', { bubbles: true }));
  last.querySelector('textarea').value = 'List the risks in {project}';
  last.querySelector('textarea').dispatchEvent(new window.Event('input', { bubbles: true }));
  [...host.querySelectorAll('button')].find((b) => b.textContent === 'Save loadouts').click();
  await waitFor(() => saved.length, { what: 'the save' });
  const sent = saved[0][0].quick_actions;
  assert.deepEqual(sent.at(-1), { label: 'List risks', prompt: 'List the risks in {project}' });
  assert.equal(sent.length, 6);
});
