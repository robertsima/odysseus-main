// Talk to agent / Assign a task (static/js/agentActions.js) on a node page.
// The server owns what is allowed; these tests pin what the sheet does with its
// answers: show the access sentence, show a blocker as a sentence plus real
// buttons, and send what the person chose when it launches.
import { after, test } from 'node:test';
import assert from 'node:assert/strict';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => { fake.restore(); await dom.restore(); });

const opened = [];
window.sessionModule = {
  getCurrentSessionId: () => 'chat-1',
  getSessions: () => [{ id: 'chat-1', name: 'Tall Tree' }],
  loadSessions: async () => {},
  selectSession: async (sid) => { opened.push(sid); },
};
document.body.innerHTML = '<textarea id="message"></textarea><div id="host"></div>';

const { default: actions } = await import('../../../../static/js/agentActions.js');

const PROFILES = { profiles: [
  { name: 'Reader', quick_actions: [{ label: 'Summarise', prompt: 'Summarise {document} for {project}' }] },
  { name: 'Writer', quick_actions: [] },
] };
const access = (extra = {}) => ({
  name: 'Reader',
  talk: { summary: 'Can read project files.', facts: [] },
  assign: { summary: 'Can read project files. Results return to this chat.', facts: [], blocker: null, ...extra },
  advanced: { tool_access: 'Selected tools', tools: ['read_file'], tool_count: 1, skills: 'all skills', shell: 'No commands', approval: 'inherit', withheld: [] },
  quick_actions: [],
});

let accessQueue = [];
const launches = [];
const state = { refuseFirst: false, blocker: null };
fake.route('GET', '/api/agents/profiles', () => PROFILES);
fake.route('GET', /^\/api\/agents\/profiles\/[^/]+\/access$/, ({ url }) => {
  const next = accessQueue.length > 1 ? accessQueue.shift() : accessQueue[0];
  return typeof next === 'function' ? next(url) : next;
});
fake.route('POST', '/api/agents/launch', ({ body }) => {
  const sent = JSON.parse(body);
  launches.push(sent);
  return launches.length === 1 && state.refuseFirst
    ? jsonResponse({ error: 'x', code: 'WORKSPACE_REQUIRED', blocker: state.blocker }, { status: 409 })
    : { run_id: 'r1', session_id: 'w1', session_name: 'Reader worker' };
});
let chatReply = () => ({ session_id: 'new-chat' });
fake.route('POST', '/api/agents/profiles/Reader/chat', () => chatReply());

function mount(opts = {}) {
  const host = document.getElementById('host');
  host.innerHTML = '';
  return actions.mountAssignSheet(host, { agent: 'Reader', parent: 'chat-1', ...opts });
}
const $q = (sel) => document.querySelector(sel);
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
function type(el, value) {
  el.value = value;
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
}

test('the sheet shows what the agent can do when assigned and where the result goes', async () => {
  accessQueue = [access()];
  mount();
  await waitFor(() => /Can read project files/.test($q('[data-aa-part="summary"]').textContent), { what: 'the summary' });
  assert.doesNotMatch($q('[data-aa-part="summary"]').textContent, /Results return/, 'said once, with the chat name');
  assert.match($q('[data-aa-part="return"]').textContent, /Results return to “Tall Tree”/);
  assert.equal($q('[data-aa-part="blocker"]').hidden, true);
});

test('a blocker shows one sentence and its recovery buttons, and each button does what it says', async () => {
  const blocker = {
    code: 'WORKSPACE_REQUIRED', summary: 'This task works on a code folder, and none is chosen.', detail: {},
    actions: [
      { id: 'choose_workspace', label: 'Choose a code folder', candidates: [{ path: '/srv/app', name: 'app' }] },
      { id: 'run_without_files', label: 'Run without files', requires: ['no_workspace'] },
    ],
  };
  const urls = [];
  accessQueue = [(url) => { urls.push(url.search); return access({ blocker: /requires=/.test(url.search) ? null : blocker }); }];
  launches.length = 0;
  mount();
  type($q('[data-aa-field="task"]'), 'Explain the code in this repo');
  await waitFor(() => !$q('[data-aa-part="blocker"]').hidden, { what: 'the blocker' });
  assert.match($q('.aa-blocker-text').textContent, /none is chosen/);
  const labels = [...document.querySelectorAll('.aa-blocker-actions button')].map((b) => b.textContent);
  assert.deepEqual(labels, ['Choose a code folder', 'Run without files']);

  click(document.querySelector('[data-aa-action="run_without_files"]'));
  await waitFor(() => urls.some((u) => /requires=no_workspace/.test(u)), { what: 'the re-check with the choice' });
  await waitFor(() => $q('[data-aa-part="blocker"]').hidden, { what: 'the blocker to clear' });
  assert.match($q('[data-aa-part="applied"]').textContent, /Running without files/);

  click($q('[data-aa="go"]'));
  await waitFor(() => launches.length, { what: 'the launch' });
  assert.deepEqual(launches.at(-1).requires, ['no_workspace']);
  assert.equal(launches.at(-1).parent_session, 'chat-1');
  assert.equal(launches.at(-1).profile, 'Reader');
});

test('choosing a code folder puts it in the re-check and the launch', async () => {
  const blocker = { code: 'WORKSPACE_REQUIRED', summary: 'No folder.', detail: {},
    actions: [{ id: 'choose_workspace', label: 'Choose a code folder', candidates: [{ path: '/srv/app', name: 'app' }, { path: '/srv/b', name: 'b' }] }] };
  const urls = [];
  accessQueue = [(url) => { urls.push(url.search); return access({ blocker: /workspace=/.test(url.search) ? null : blocker }); }];
  launches.length = 0;
  mount();
  type($q('[data-aa-field="task"]'), 'Explain the code in this repo');
  await waitFor(() => !$q('[data-aa-part="blocker"]').hidden, { what: 'the blocker' });
  click(document.querySelector('[data-aa-action="choose_workspace"]'));
  const picker = $q('[data-aa-pick="workspace"]');
  assert.deepEqual([...picker.options].map((o) => o.textContent), ['app', 'b']);
  picker.value = '/srv/b';
  click($q('[data-aa="use-folder"]'));
  await waitFor(() => urls.some((u) => /workspace=%2Fsrv%2Fb/.test(u)), { what: 'the re-check with the folder' });
  click($q('[data-aa="go"]'));
  await waitFor(() => launches.length, { what: 'the launch' });
  assert.equal(launches[0].workspace, '/srv/b');
});

test('allow for this run sends exactly the tools the server offered, and nothing else', async () => {
  const blocker = { code: 'DOCUMENT_UNREADABLE', summary: 'This agent cannot open the document.', detail: {},
    actions: [{ id: 'allow_for_this_run', label: 'Allow for this run only', extra_tools: ['manage_documents'] },
      { id: 'attach_excerpt', label: 'Paste an excerpt' }] };
  accessQueue = [(url) => access({ blocker: /extra_tools=/.test(url.search) ? null : blocker })];
  launches.length = 0;
  mount();
  type($q('[data-aa-field="task"]'), 'Revise the editor document DA-02');
  await waitFor(() => !$q('[data-aa-part="blocker"]').hidden, { what: 'the blocker' });
  click(document.querySelector('[data-aa-action="allow_for_this_run"]'));
  await waitFor(() => $q('[data-aa-part="blocker"]').hidden, { what: 'the blocker to clear' });
  click($q('[data-aa="go"]'));
  await waitFor(() => launches.length, { what: 'the launch' });
  assert.deepEqual(launches[0].extra_tools, ['manage_documents']);
});

test('a pasted excerpt travels inside the task, not as a separate field', async () => {
  const blocker = { code: 'DOCUMENT_UNREADABLE', summary: 'Cannot open it.', detail: {},
    actions: [{ id: 'attach_excerpt', label: 'Paste an excerpt' }] };
  accessQueue = [access({ blocker })];
  launches.length = 0;
  mount();
  type($q('[data-aa-field="task"]'), 'Revise the editor document DA-02');
  await waitFor(() => !$q('[data-aa-part="blocker"]').hidden, { what: 'the blocker' });
  click(document.querySelector('[data-aa-action="attach_excerpt"]'));
  assert.equal($q('[data-aa-part="excerpt"]').hidden, false);
  type($q('[data-aa-field="excerpt"]'), 'Clause 4: payment within 30 days.');
  click($q('[data-aa="go"]'));
  await waitFor(() => launches.length, { what: 'the launch' });
  assert.equal(launches[0].task, 'Revise the editor document DA-02\n\nExcerpt:\n```\nClause 4: payment within 30 days.\n```');
});

test('a launch the server refuses with a blocker shows it in the sheet instead of an error string', async () => {
  state.refuseFirst = true;
  state.blocker = { code: 'WORKSPACE_REQUIRED', summary: 'No folder chosen.', detail: {},
    actions: [{ id: 'run_without_files', label: 'Run without files', requires: ['no_workspace'] }] };
  accessQueue = [access()];
  launches.length = 0;
  mount();
  type($q('[data-aa-field="task"]'), 'Explain the code in this repo');
  click($q('[data-aa="go"]'));
  await waitFor(() => !$q('[data-aa-part="blocker"]').hidden, { what: 'the refusal blocker' });
  assert.match($q('.aa-blocker-text').textContent, /No folder chosen/);
  assert.equal($q('[data-aa-part="msg"]').textContent, '');
  state.refuseFirst = false;
});

test('Talk to agent resumes through the route and opens it', async () => {
  const sid = await actions.talkToAgent('Reader');
  assert.equal(sid, 'new-chat');
  assert.deepEqual(opened, ['new-chat']);
  const call = fake.calls.find((c) => c.method === 'POST' && c.url.pathname === '/api/agents/profiles/Reader/chat');
  assert.deepEqual(JSON.parse(call.body), { from_session: 'chat-1', new_chat: false });
});

test('New chat explicitly requests another conversation', async () => {
  await actions.talkToAgent('Reader', { newChat: true });
  const call = fake.calls.filter((c) => c.method === 'POST' && c.url.pathname === '/api/agents/profiles/Reader/chat').at(-1);
  assert.equal(JSON.parse(call.body).new_chat, true);
});

for (const customOpen of [false, true]) test(`a delayed chat open preserves navigation with custom callback=${customOpen}`, async () => {
  const previous = window.sessionModule.getCurrentSessionId;
  let current = 'chat-1';
  window.sessionModule.getCurrentSessionId = () => current;
  const count = opened.length;
  let finish;
  chatReply = () => new Promise((resolve) => { finish = resolve; });
  try {
    const pending = actions.talkToAgent('Reader', customOpen ? { open: async (sid) => opened.push(sid) } : {});
    await waitFor(() => finish, { what: 'pending chat navigation' });
    current = 'another-chat';
    finish({ session_id: 'new-chat' });
    await pending;
    assert.equal(opened.length, count);
  } finally {
    window.sessionModule.getCurrentSessionId = previous;
    chatReply = () => ({ session_id: 'new-chat' });
  }
});

test('quick actions fill the composer, with placeholders filled from the chat context', async () => {
  window.chatSettingsModule = { getContext: () => ({ project: 'Tall Tree Group', document: { title: 'DA-02' } }) };
  const slot = document.createElement('div');
  document.body.appendChild(slot);
  const list = await actions.renderQuickActions(slot, 'Reader');
  assert.equal(list.length, 1);
  assert.equal(slot.hidden, false);
  click(slot.querySelector('.aa-chip'));
  assert.equal($q('#message').value, 'Summarise DA-02 for Tall Tree Group');
  const none = document.createElement('div');
  await actions.renderQuickActions(none, 'Writer');
  assert.equal(none.hidden, true);
});

test('a placeholder with no context is left as written', () => {
  assert.equal(actions.fillPlaceholders('Review {document}', { project: '', document: '' }), 'Review {document}');
});
