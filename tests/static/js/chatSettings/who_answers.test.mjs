// The context strip says who answers the chat (static/js/chatSettings.js
// identity, render). 2026-10-20: a chat running as a loadout's agent showed
// "All tools" and nothing about the agent, its persona or its delegation, so
// the settings looked the same whichever persona was in use.
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

document.body.innerHTML = '<div id="chat-container"><div class="chat-top-bar"></div><div id="chat-history"></div></div><div class="chat-input-bar"></div>';
let openChat = null;
window.sessionModule = { getCurrentSessionId: () => openChat };
const saved = {
  admin: {
    agent_profile: 'Odysseus Admin', agent_persona_name: 'Odysseus', delegation_policy: 'auto',
    tool_access: 'selected', enabled_tools: ['read_file', 'mcp__penpot__*'],
  },
  plain: {},
};
fake.route('GET', /^\/api\/session\/[^/]+\/settings$/, ({ url }) => ({ settings: saved[url.pathname.split('/')[3]] || {} }));
fake.route('GET', /^\/api\/tools$/, () => ({ tools: [
  { id: 'read_file' }, { id: 'bash' }, { id: 'mcp__penpot__get_file' }, { id: 'mcp__github__get_me' },
] }));

const chatSettings = (await import('../../../../static/js/chatSettings.js')).default;
const presets = await import('../../../../static/js/presets.js');

async function open(id, { agentMode }) {
  document.body.classList.toggle('composer-agent-mode', agentMode);
  openChat = id;
  await chatSettings.onSessionSwitch(id);
}
const line = () => document.getElementById('chat-status-line').textContent;

test('an agent chat names its agent, delegation and loadout tools', async () => {
  await open('admin', { agentMode: true });

  assert.match(line(), /Odysseus/);
  assert.match(line(), /Change/);
});

test('in Chat mode the shared Prompt persona is the one named', async () => {
  presets.getAllPresets().custom = { character_name: 'Ada', enabled: true };
  presets.setActivePreset('custom');
  await open('admin', { agentMode: false });

  assert.match(line(), /Persona: Ada/);
  assert.doesNotMatch(line(), /Odysseus/);
  presets.setActivePreset(null);
});

test('the panel greys out tools the loadout does not allow', async () => {
  await open('admin', { agentMode: true });
  document.querySelector('[data-csl="panel-agent"]').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));

  const panel = document.getElementById('chat-settings-panel');
  assert.ok(panel, 'panel opened');
  assert.equal(panel.parentElement, document.body, 'floats outside the composer column');
  const box = (name) => panel.querySelector(`input[data-tool="${name}"]`);
  assert.equal(box('read_file').disabled, false);
  assert.equal(box('mcp__penpot__get_file').disabled, false);
  assert.equal(box('bash').disabled, true);
  assert.equal(box('mcp__github__get_me').disabled, true);
  assert.equal(panel.querySelector('input[name="csp-delegation"]:checked').value, 'auto');
});
