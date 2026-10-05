// Startup with a failing session list: static/js/sessions.js loadSessions()
// and the startup sequencing in static/js/startupShell.js. A 503 from
// /api/sessions is a failure, not an empty account: the page must keep the
// chats it has, must not create a fresh default chat (which wipes the draft
// and the remembered chat), and must leave the sidebar saying the chats are
// unavailable, with a route that needs them (#/email) still waiting. A 401
// belongs to the app's sign-in redirect and shows no error of its own.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const answers = [
  () => [{ id: 'existing', name: 'Existing', folder: 'Assistant', archived: false }],
  () => jsonResponse({ detail: 'temporarily unavailable' }, { status: 503 }),
  () => jsonResponse({ detail: 'expired' }, { status: 401 }),
];
fake.route('GET', '/api/sessions', () => answers.shift()());

document.body.innerHTML = `
  <div id="app-loader"></div>
  <div id="sessions-section"><div id="session-list"></div></div>
  <div id="session-list-loading"><span data-session-list-status>Loading chats…</span></div>
  <textarea id="message"></textarea>
  <div id="toast"></div>`;
window.__odysseusDefaultChat = { endpoint_url: 'http://model.test/v1', model: 'test/model', endpoint_id: 'endpoint-1' };

const sessions = (await import('../../../../static/js/sessions.js')).default;
const shell = await import('../../../../static/js/startupShell.js');

const toast = () => document.getElementById('toast').textContent;
const message = document.getElementById('message');
let routeOpened = 0;
let hydrated;

test('a 503 session list keeps the chats already loaded and creates no new chat', async () => {
  assert.equal(await sessions.loadSessions(), true);
  localStorage.setItem('lastSessionId', 'existing');
  message.value = 'draft must survive';
  shell.deferRouteOpener('/email', () => { routeOpened += 1; });

  hydrated = await shell.settleSessionHydration(() => sessions.loadSessions());

  assert.equal(hydrated, false);
  assert.deepEqual(sessions.getSessions().map((s) => s.id), ['existing']);
  assert.equal(sessions.hasPendingChat(), false, 'the failure created a default chat');
  assert.equal(message.value, 'draft must survive');
  assert.equal(localStorage.getItem('lastSessionId'), 'existing');
  assert.match(toast(), /^Failed to load sessions: temporarily unavailable/);
});

test('the sidebar says the chats are unavailable and the #/email route stays unopened', async () => {
  const status = document.querySelector('#session-list-loading [data-session-list-status]');
  await waitFor(() => status.textContent === 'Chats unavailable', { what: 'the failure row' });
  await waitFor(() => !document.getElementById('app-loader'), { what: 'the loader to go' });

  assert.ok(document.getElementById('session-list-loading'), 'the failure row stays');
  assert.equal(shell.runDeferredRouteOpener({ sessionsSettled: true }), false);
  assert.equal(routeOpened, 0);
});

test('a 401 session list shows no error of its own', async () => {
  document.getElementById('toast').textContent = '';

  assert.equal(await sessions.loadSessions(), false);

  assert.equal(toast(), '');
  assert.deepEqual(sessions.getSessions().map((s) => s.id), ['existing']);
});
