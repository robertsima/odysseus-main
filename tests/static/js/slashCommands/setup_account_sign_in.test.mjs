// /setup account sign-in in static/js/slashCommands.js.
// - The setup guide offers the device-auth providers (GitHub Copilot,
//   ChatGPT Subscription) and clicking one fills the composer with its
//   `/setup <provider>` command. Treating them like API providers filled in
//   "<name> sk-", asking for a key these providers do not use.
// - `/setup chatgpt-subscription` shows the sign-in URL and code in the chat
//   and does not open a tab on its own.
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

const AUTH_URL = 'https://auth.openai.com/codex/device';
fake.route('POST', '/api/chatgpt-subscription/device/start', () => ({
  poll_id: 'p1', user_code: 'ABCD-1234', verification_uri: AUTH_URL, interval: 5, expires_in: 900,
}));
fake.route('POST', '/api/chatgpt-subscription/device/poll', () => new Promise(() => {}));

document.body.innerHTML = `
  <div id="chat-history"></div>
  <div id="models"><div class="models-row"></div></div>
  <form id="chat-form"><textarea id="message"></textarea></form>
`;

const opened = [];
window.open = (...args) => { opened.push(args); return null; };

const { initSlashCommands, handleSlashCommand } = await import('../../../../static/js/slashCommands.js');
initSlashCommands({ apiBase: '' });

const composer = document.getElementById('message');

function chipsOfKind(kind) {
  return [...document.querySelectorAll(`.setup-clickable-provider[data-setup-kind="${kind}"]`)];
}

test('the setup guide offers account sign-in providers that fill in their /setup command', async () => {
  await handleSlashCommand('/setup endpoint');
  const accountChips = chipsOfKind('device-auth');

  const commands = accountChips.map((chip) => {
    chip.click();
    return composer.value;
  });
  assert.deepEqual(commands.sort(), ['/setup chatgpt-subscription', '/setup copilot']);
});

test('an API provider chip still asks for its key', () => {
  const deepseek = chipsOfKind('api-key').find((chip) => chip.dataset.setupProvider === 'deepseek');
  deepseek.click();
  assert.equal(composer.value, 'deepseek sk-');
});

test('/setup chatgpt-subscription shows the sign-in link and code without opening a tab', async () => {
  // The flow then polls every few seconds until the user signs in; the test
  // looks at what it shows before the first poll.
  handleSlashCommand('/setup chatgpt-subscription');
  let link;
  await waitFor(() => (link = document.querySelector(`#chat-history a[href="${AUTH_URL}"]`)),
    { what: 'the sign-in link' });

  assert.equal(link.target, '_blank');
  assert.ok(link.closest('.msg').textContent.includes('ABCD-1234'), 'the device code is shown');
  // The runner opens the URL right after showing it, if it is going to.
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(opened, []);
});
