// The context strip shows the answering agent's quick actions and hands the
// chat's project and document to their placeholders. The strip and the chips
// were built separately (chatSettings.js / agentActions.js); this pins the seam.
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
    <div class="chat-top-bar"></div>
    <div id="chat-history"></div>
  </div>
  <textarea id="message"></textarea>`;
document.body.classList.add('composer-agent-mode');
let openChat = null;
window.sessionModule = { getCurrentSessionId: () => openChat };
window.documentModule = { getCurrentDocId: () => 'doc-1', getCurrentDocTitle: () => 'DA-02' };

fake.route('GET', /^\/api\/session\/[^/]+\/settings$/, () => ({
  settings: { agent_profile: 'SDS Consulting Lead', doc_target: 'document' },
  approval_mode: 'ask_risky', approval_modes: ['auto', 'ask_risky', 'ask_all'], shell_access: 'sandbox',
  context: { who: { kind: 'agent', name: 'SDS Consulting Lead' }, project: 'Tall Tree Group',
    document: { id: 'doc-1', title: 'DA-02' }, doc_target: 'document',
    access: { summary: 'File editing allowed · External actions require approval' } },
}));
fake.route('GET', /^\/api\/agents\/profiles$/, () => ({
  profiles: [{ name: 'SDS Consulting Lead', quick_actions: [{ label: 'Revise agreement', prompt: 'Revise {document} for {project}.' }] }],
}));

await import('../../../../static/js/agentActions.js');
const chatSettings = (await import('../../../../static/js/chatSettings.js')).default;

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test('the answering agent\'s quick actions appear in the strip and fill the composer', async () => {
  openChat = 's1';
  await chatSettings.onSessionSwitch('s1');
  for (let i = 0; i < 5; i += 1) await settle();

  const chips = [...document.querySelectorAll('#chat-status-line .ctx-quick-actions button')];
  assert.deepEqual(chips.map((c) => c.textContent), ['Revise agreement']);

  chips[0].click();
  assert.equal(document.getElementById('message').value, 'Revise DA-02 for Tall Tree Group.');
});

test('getContext gives the project and the open document', () => {
  assert.deepEqual(chatSettings.getContext(), { project: 'Tall Tree Group', document: { id: 'doc-1', title: 'DA-02' } });
});
