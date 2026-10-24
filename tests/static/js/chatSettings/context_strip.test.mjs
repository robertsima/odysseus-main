// The context strip (static/js/chatSettings.js): one or two lines above the
// conversation that say who answers, which project, which document and what the
// chat may do, plus the selector that decides what an open document stands for.
// 2026-10-23: that line sat under the composer, and opening a document silently
// made every message about it.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
globalThis.setInterval = () => 0;
after(async () => {
  fake.restore();
  await dom.restore();
});

document.body.innerHTML = `
  <div id="chat-container">
    <div class="chat-top-bar"><button id="chat-context-pill" hidden>42%</button></div>
    <div id="chat-history"></div>
  </div>
  <div class="chat-input-bar"></div>
  <button id="character-indicator-btn" style="display:none"></button>
  <button id="workspace-indicator-btn" style="display:none"><span id="workspace-indicator-name"></span></button>`;
let openChat = null;
let openDoc = null; // { id, title } shown in the editor
window.sessionModule = { getCurrentSessionId: () => openChat };
window.documentModule = {
  getCurrentDocId: () => (openDoc ? openDoc.id : null),
  getCurrentDocTitle: () => (openDoc ? openDoc.title : ''),
};

const AUTO = 'File editing allowed · Actions run without asking';
const payloads = {
  fresh: { settings: {}, approval_mode: 'auto', approval_modes: ['auto', 'ask_risky', 'ask_all'], shell_access: 'sandbox',
    context: { who: { kind: 'default', name: null }, project: null, document: null, doc_target: 'general', access: { summary: AUTO } } },
  tall: { settings: {}, approval_mode: 'ask_risky', approval_modes: ['auto', 'ask_risky', 'ask_all'], shell_access: 'sandbox',
    context: { who: { kind: 'default', name: null }, project: 'Tall Tree Group', document: null, doc_target: 'general',
      access: { summary: 'File editing allowed · External actions require approval' } } },
  saved: { settings: { doc_target: 'project' }, approval_mode: 'auto', approval_modes: ['auto'], shell_access: 'sandbox',
    context: { who: { kind: 'default', name: null }, project: 'Tall Tree Group', document: null, doc_target: 'project', access: { summary: AUTO } } },
};
const patches = [];
fake.route('GET', /^\/api\/session\/[^/]+\/settings$/, ({ url }) => payloads[url.pathname.split('/')[3]]);
fake.route('PATCH', /^\/api\/session\/[^/]+\/settings$/, ({ url, body }) => {
  const patch = typeof body === 'string' ? JSON.parse(body) : body;
  patches.push(patch);
  const base = payloads[url.pathname.split('/')[3]];
  return { ...base, settings: { ...base.settings, ...patch } };
});

const chatSettings = (await import('../../../../static/js/chatSettings.js')).default;

async function open(id, doc = null) {
  openDoc = doc;
  openChat = id;
  await chatSettings.onSessionSwitch(id);
  await new Promise((resolve) => setTimeout(resolve, 0));
}
const strip = () => document.getElementById('chat-status-line');
const lines = () => [...strip().querySelectorAll('.ctx-l1, .ctx-l2')].map((n) => n.textContent.replace(/\s+/g, ' ').trim());
const segment = () => strip().querySelector('.ctx-seg');

test('the strip sits above the history, right after the top bar', async () => {
  await open('tall');

  assert.equal(strip().previousElementSibling.className, 'chat-top-bar');
  assert.equal(strip().nextElementSibling.id, 'chat-history');
});

test('line one names the project and line two says what the chat may do', async () => {
  await open('tall');

  const [one, two] = lines();
  assert.match(one, /Tall Tree Group/);
  assert.match(one, /Change$/);
  assert.equal(two, 'File editing allowed · External actions require approval');
});

test('a brand-new default chat has nothing to say, so the strip is hidden', async () => {
  await open('fresh');

  assert.equal(strip().hidden, true);
});

test('a document opened in this chat shows the selector, aimed at the document', async () => {
  await open('tall');
  openDoc = { id: 'd1', title: 'DA-02' };
  chatSettings.render();

  assert.match(lines()[0], /Tall Tree Group.+Document: DA-02/);
  const picked = segment().querySelector('[aria-checked="true"]');
  assert.equal(picked.textContent, 'Include document');
  assert.deepEqual([...segment().querySelectorAll('button')].map((b) => b.textContent),
    ['Include document', 'Leave document out']);
  assert.deepEqual(chatSettings.docTarget(), { docId: 'd1', target: 'document' });
});

test('a document that was already open on arrival defaults to a general conversation', async () => {
  await open('tall', { id: 'carried', title: 'Other' });

  assert.equal(segment().querySelector('[aria-checked="true"]').textContent, 'Leave document out');
  assert.doesNotMatch(lines()[0], /Editing/);
  assert.equal(chatSettings.docTarget().target, 'general');
});

test('with no document open there is no selector', async () => {
  await open('tall');

  assert.equal(segment(), null);
  assert.equal(chatSettings.docTarget().docId, null);
});

test('choosing a target saves it on the chat and follows it at once', async () => {
  await open('tall');
  openDoc = { id: 'd2', title: 'DA-03' };
  chatSettings.render();
  patches.length = 0;

  segment().querySelector('[data-target="general"]').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(patches, [{ doc_target: 'general' }]);
  assert.equal(segment().querySelector('[aria-checked="true"]').textContent, 'Leave document out');
  assert.doesNotMatch(lines()[0], /Editing/);
  assert.equal(chatSettings.docTarget().target, 'general');
});

test('a saved target wins over the default', async () => {
  await open('saved');
  openDoc = { id: 'd3', title: 'DA-04' };
  chatSettings.render();

  assert.equal(segment().querySelector('[aria-checked="true"]').textContent, 'Leave document out');
});

test('the persona, workspace and context-use indicators live in the strip', async () => {
  await open('tall');

  const slot = strip().querySelector('.ctx-slot');
  for (const id of ['chat-context-pill', 'character-indicator-btn', 'workspace-indicator-btn']) {
    assert.equal(document.getElementById(id).parentElement, slot, `${id} moved into the strip`);
  }
});

test('a shown context indicator keeps the strip visible in an otherwise empty chat', async () => {
  await open('fresh');
  document.getElementById('chat-context-pill').hidden = false;
  chatSettings.render();

  assert.equal(strip().hidden, false);
  document.getElementById('chat-context-pill').hidden = true;
});

test('the empty quick-actions slot is there for the Agents panel', async () => {
  await open('tall');

  assert.ok(strip().querySelector('.ctx-quick-actions'));
});

test('document inclusion supports arrow navigation and retains focus after saving', async () => {
  await open('tall', { id: 'keyboard', title: 'Keyboard draft' });
  const excluded = segment().querySelector('[data-target="general"]');
  excluded.focus();
  excluded.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(chatSettings.docTarget().target, 'document');
  assert.equal(document.activeElement.dataset.target, 'document');
  assert.equal(document.activeElement.getAttribute('aria-checked'), 'true');
});
