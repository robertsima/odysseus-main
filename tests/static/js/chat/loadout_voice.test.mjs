// The shared prompt from the Prompt window (its persona name and its inject
// prefix and suffix) applies to plain chats. An agent chat running under a
// loadout has its own voice, so static/js/chat.js must send the message
// without the shared inject text and label the reply with the loadout's
// persona (static/js/agentMenu.js says which applies).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
// app.js publishes the session module; agentMenu.js reads it from window.
window.sessionModule = page.sessionModule;
let chatSettings = {};
page.fake.route('GET', '/api/presets', () => ({
  custom: {
    enabled: true, character_name: 'Socrates', system_prompt: 'Answer with questions.',
    inject_prefix: '[shared-prefix]', inject_suffix: '[shared-suffix]',
  },
}));
page.fake.route('GET', '/api/session/s1/settings', () => ({ settings: chatSettings }));
page.fake.route('GET', '/api/agents/profiles', () => ({ profiles: [] }));

const presets = await import('../../../../static/js/presets.js');
await presets.loadPresets();
// The composer's Agents entry and the Agent mode button, which agentMenu.js
// wires when it loads.
document.body.insertAdjacentHTML('beforeend', '<button id="overflow-agents-btn"></button><button id="mode-agent-btn"></button>');
const agentMenu = (await import('../../../../static/js/agentMenu.js')).default;

async function turn(text) {
  const stream = page.streamReply();
  const form = await page.send(text);
  const bubble = [...page.box.querySelectorAll('.msg-ai')].at(-1);
  const label = bubble.querySelector('.role').textContent;
  stream.event({ delta: 'Answer.' });
  stream.done();
  await page.settled();
  return { message: form.get('message'), label };
}

test('a plain chat sends the shared inject text and answers as the shared persona', async () => {
  const { message, label } = await turn('Hello');
  assert.equal(message, '[shared-prefix] Hello [shared-suffix]');
  assert.match(label, /^Socrates/);
});

test('an agent chat under a loadout drops the shared inject text and answers as its own persona', async () => {
  document.getElementById('mode-agent-btn').classList.add('active');
  chatSettings = { agent_profile: 'Scout', agent_persona_name: 'Scout' };
  document.dispatchEvent(new CustomEvent('odysseus:loadout-changed', { detail: { sessionId: 's1' } }));
  await waitFor(() => agentMenu.sharedPersonaSuppressed(), { what: 'the loadout voice to load' });

  const { message, label } = await turn('Hello');
  assert.equal(message, 'Hello');
  assert.match(label, /^Scout/);
});
