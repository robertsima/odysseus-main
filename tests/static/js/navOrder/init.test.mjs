// First paint and account sync. A browser holding only the v1 flat order must
// open with the same placement it had; a newer account layout is drawn when it
// arrives, nodes and all.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';
import { buildNavDom, groupIds, rowIds } from '../_support/navFixture.mjs';

const dom = installDom();
after(() => dom.restore());

const flat = ['search', 'new-chat', 'command', 'tasks', 'workbench', 'agents', 'gallery', 'memory', 'today',
  'calendar', 'compare', 'cookbook', 'research', 'email', 'library', 'lotus', 'notes', 'theme'];
localStorage.setItem('odysseus-nav-order-v1', JSON.stringify({ value: flat, updated_at: 5 }));
localStorage.setItem('section-collapsed', JSON.stringify({ 'more-section': false }));

const account = new Map();
const fake = installFetchFake();
fake.route('GET', /^\/api\/prefs\/[\w-]+$/, ({ url }) => ({ value: account.get(url.pathname.split('/').pop()) ?? null }));
fake.route('PUT', /^\/api\/prefs\/[\w-]+$/, ({ url, body }) => { account.set(url.pathname.split('/').pop(), JSON.parse(body).value); return jsonResponse({ ok: true }); });

buildNavDom();
const tasksRow = document.getElementById('tool-tasks-btn');
await import('../../../../static/js/navOrder.js');

test('a browser that only has the v1 order opens with its placement and fold state', () => {
  assert.deepEqual(rowIds(document, 'work'), ['tool-tasks-btn', 'tool-today-btn', 'tool-calendar-btn', 'email-section', 'tool-library-btn', 'tool-notes-btn']);
  assert.deepEqual(rowIds(document, 'agents'), ['tool-workbench-btn', 'tool-agents-btn']);
  assert.deepEqual(rowIds(document, 'more')[0], 'tool-gallery-btn');
  assert.equal(document.getElementById('more-section').classList.contains('collapsed'), false);
  assert.equal(document.getElementById('tool-tasks-btn'), tasksRow, 'the original node was moved, not rebuilt');
});

test('the v1 order is migrated to the account so a second browser gets it', async () => {
  await waitFor(() => account.has('nav-layout'), { what: 'the migrated layout to reach the account' });
  assert.deepEqual(account.get('nav-layout').value.groups.find((g) => g.id === 'work').items.slice(0, 2), ['tasks', 'today']);
});

test('a newer account layout arriving later is drawn', async () => {
  const L = await import('../../../../static/js/navLayout.js');
  const theirs = L.defaultLayout();
  theirs.groups.reverse();
  theirs.groups[0].label = 'Reversed';
  const entry = { value: theirs, updated_at: Date.now() + 60_000 };
  account.set('nav-layout', entry);
  await L.syncLayoutWithAccount();
  assert.deepEqual(groupIds(document), ['more', 'agents', 'work']);
  assert.equal(document.querySelector('#more-section .nav-group-title').textContent, 'Reversed');
});
