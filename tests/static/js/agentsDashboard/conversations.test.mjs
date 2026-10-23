// Phalanx keeps agent conversations in a stable, user-ordered list. A status
// change must not move a row or drop the selection, the order is saved per
// account (serverPrefs, key `phalanx-order`), and "New agent conversation"
// starts a Talk to agent chat without turning it into an assigned task.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { jsonResponse } from '../_support/fetchFake.mjs';
import { openRoom, waitFor } from './_room.mjs';

const conv = (n, extra = {}) => ({
  session_id: `c${n}`, name: `Conversation ${n}`, conversation: true, status: 'idle', outcome: 'done',
  availability: 'idle', created_at: 100 - n, source: 'session', child_activity: 0, config: {}, ...extra,
});

const room = await openRoom({
  rows: [conv(1), conv(2), conv(3)],
  profiles: [{ name: 'Reader', description: 'Reads files' }],
  chats: [{ id: 'c1', name: 'Conversation 1' }],
});
const { dashboard, root, fake } = room;
const prefs = [];
fake.route('GET', /^\/api\/prefs\//, () => jsonResponse({}, { status: 404 }));
fake.route('PUT', /^\/api\/prefs\//, ({ url, body }) => { prefs.push({ key: decodeURIComponent(url.pathname.split('/').pop()), body: JSON.parse(body) }); return { ok: true }; });
fake.route('POST', '/api/agents/profiles/Reader/chat', () => ({ session_id: 'c9', name: 'Reader', model: 'm' }));

const names = () => [...root.querySelectorAll('.ag-group-conv .ag-row .ag-row-name')].map((n) => n.textContent);
const selected = () => root.querySelector('.ag-row.active')?.dataset.sid;
const refresh = async () => {
  const before = fake.calls.filter((c) => c.url.pathname === '/api/agents/overview').length;
  room.click('[data-ag="refresh"]');
  await waitFor(() => fake.calls.filter((c) => c.url.pathname === '/api/agents/overview').length > before, { what: 'a poll' });
  await new Promise((r) => setTimeout(r, 20));
};

test('an overview refresh that changes the selected row\'s status keeps it selected and in place', async () => {
  dashboard.open({ select: 'c2' });
  await waitFor(() => selected() === 'c2', { what: 'c2 selected' });
  const order = names();
  assert.deepEqual(order, ['Conversation 1', 'Conversation 2', 'Conversation 3']);

  room.overview.rows = [conv(1), conv(2, { status: 'failed', outcome: 'failed' }), conv(3, { status: 'running', outcome: null })];
  await refresh();
  assert.equal(selected(), 'c2');
  assert.deepEqual(names(), order, 'status changes do not reorder');

  room.overview.rows = [conv(1), conv(2, { status: 'idle', outcome: 'done' }), conv(3)];
  await refresh();
  assert.equal(selected(), 'c2');
  dashboard.close();
});

test('an idle conversation shows how its last run ended, apart from Idle', async () => {
  room.overview.rows = [conv(1, { outcome: 'blocked', status: 'idle' }), conv(2), conv(3)];
  dashboard.open({ select: 'c1' });
  await refresh();
  await waitFor(() => root.querySelector('.ag-row[data-sid="c1"] .ag-row-outcome'), { what: 'the outcome' });
  const card = root.querySelector('.ag-row[data-sid="c1"]');
  assert.match(card.querySelector('.wb-pill').textContent, /Idle/);
  assert.match(card.querySelector('.ag-row-outcome').textContent, /Blocked/);
  assert.equal(root.querySelector('.ag-row[data-sid="c2"] .ag-row-outcome')?.textContent.includes('Done'), true);
  dashboard.close();
});

test('filtering to Needs you keeps the selected conversation listed and selected', async () => {
  room.overview.rows = [conv(1), conv(2), conv(3, { status: 'needs_input', outcome: 'needs_input', needs: ['pick one'] })];
  dashboard.open({ select: 'c1' });
  await refresh();
  await waitFor(() => selected() === 'c1', { what: 'c1 selected' });
  room.click('[data-ag="bucket"][data-bucket="attention"]');
  assert.equal(selected(), 'c1');
  assert.deepEqual(names(), ['Conversation 1', 'Conversation 3']);
  room.click('[data-ag="bucket"][data-bucket="attention"]');
  dashboard.close();
});

test('moving a conversation down saves the order for the account and survives a refresh', async () => {
  room.overview.rows = [conv(1), conv(2), conv(3)];
  dashboard.open({ select: 'c1' });
  await refresh();
  await waitFor(() => names().length === 3, { what: 'the list' });
  assert.equal(root.querySelector('.ag-row[data-sid="c1"] [data-ag="move-up"]').disabled, true);
  root.querySelector('.ag-row[data-sid="c1"] [data-ag="move-down"]').click();
  assert.deepEqual(names(), ['Conversation 2', 'Conversation 1', 'Conversation 3']);
  await waitFor(() => prefs.length, { what: 'the saved order' });
  assert.equal(prefs.at(-1).key, 'phalanx-order');
  assert.deepEqual(prefs.at(-1).body.value.value, ['c2', 'c1', 'c3']);

  // A conversation deleted elsewhere is dropped, a new one lands on top.
  room.overview.rows = [conv(1), conv(3), conv(4, { created_at: 500 })];
  await refresh();
  assert.deepEqual(names(), ['Conversation 4', 'Conversation 1', 'Conversation 3']);
  dashboard.close();
});

test('New agent conversation starts a Talk to agent chat and selects it', async () => {
  room.overview.rows = [conv(1)];
  dashboard.open({ select: 'c1' });
  await refresh();
  await waitFor(() => root.querySelector('[data-ag="new-conversation"]'), { what: 'the header button' });
  room.click('[data-ag="new-conversation"]');
  await waitFor(() => root.querySelector('[data-ag="new-conversation-pick"]'), { what: 'the agent picker' });
  room.overview.rows = [conv(1), conv(9, { name: 'Reader', created_at: 900 })];
  room.click('[data-ag="new-conversation-pick"][data-profile="Reader"]');
  await waitFor(() => selected() === 'c9', { what: 'the new conversation selected' });
  assert.ok(fake.calls.some((c) => c.method === 'POST' && c.url.pathname === '/api/agents/profiles/Reader/chat'));
  assert.ok(!fake.calls.some((c) => c.url.pathname === '/api/agents/launch'), 'no task was assigned');
  assert.equal(root.querySelector('#ag-newconv'), null, 'the picker closes');
  dashboard.close();
});
