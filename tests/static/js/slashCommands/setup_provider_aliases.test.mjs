// The setup wizard recognises the OpenCode providers by the words a person
// types ("opencode zen sk-...", "opencode-go") and connects each to its own URL.
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

fake.route('POST', '/api/model-endpoints', () => ({ id: 'e1', models: [] }));

document.body.innerHTML = `
  <div id="chat-history"></div>
  <div id="models"><div class="models-row"></div></div>
  <form id="chat-form"><textarea id="message"></textarea></form>
`;

const { initSlashCommands, handleSetupWizard } = await import('../../../../static/js/slashCommands.js');
initSlashCommands({ apiBase: '' });

async function connectedEndpoint(run) {
  const before = fake.calls.length;
  run();
  let call;
  await waitFor(() => (call = fake.calls.slice(before).find((c) => c.url.pathname === '/api/model-endpoints')),
    { what: 'the endpoint request' });
  return { url: call.body.get('base_url'), key: call.body.get('api_key'), name: call.body.get('name') };
}

async function repliedWith(text, run) {
  run();
  await waitFor(() => document.getElementById('chat-history').textContent.includes(text), { what: text });
}

test('the bare name "opencode-go" asks for the OpenCode Go key', async () => {
  await repliedWith('Paste your OpenCode Go API key', () => handleSetupWizard('endpoint-provider-first', 'opencode-go'));
});

test('the bare words "opencode zen" ask for the OpenCode Zen key', async () => {
  await repliedWith('Paste your OpenCode Zen API key', () => handleSetupWizard('endpoint-provider-first', 'opencode zen'));
});

test('"opencode zen <key>" typed into the wizard connects OpenCode Zen', async () => {
  const sent = await connectedEndpoint(() => handleSetupWizard('endpoint-provider-first', 'opencode zen sk-test'));
  assert.deepEqual(sent, { url: 'https://opencode.ai/zen/v1', key: 'sk-test', name: 'OpenCode Zen' });
});

test('"opencode go <key>" typed into the wizard connects OpenCode Go', async () => {
  const sent = await connectedEndpoint(() => handleSetupWizard('endpoint-provider-first', 'opencode go sk-test'));
  assert.deepEqual(sent, { url: 'https://opencode.ai/zen/go/v1', key: 'sk-test', name: 'OpenCode Go' });
});
