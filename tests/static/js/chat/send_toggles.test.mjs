// The shell and web toggles in the chat send of static/js/chat.js. The server
// treats a missing allow_bash as "use the account's default", which is on for
// most users, and reads a missing allow_web_search in agent mode the same way
// (routes/chat_routes.py). So a toggle the user switched off only holds if
// the page sends the "false" explicitly.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage } from './_chatPage.mjs';

const page = await openChatPage();

async function sendWith({ bash, web, mode }) {
  localStorage.setItem('odysseus-toggles', JSON.stringify({ mode }));
  document.getElementById('bash-toggle').checked = bash;
  document.getElementById('web-toggle').checked = web;
  const stream = page.streamReply();
  const form = await page.send('what is the capital of France');
  stream.event({ delta: 'Paris.' });
  stream.done();
  await page.settled();
  return form;
}

test('a send says allow_bash=false or true to match the shell toggle', async () => {
  const off = await sendWith({ bash: false, web: false, mode: 'chat' });
  const on = await sendWith({ bash: true, web: false, mode: 'chat' });

  assert.equal(off.get('allow_bash'), 'false');
  assert.equal(on.get('allow_bash'), 'true');
});

test('an agent send says allow_web_search=false or true to match the web toggle', async () => {
  const off = await sendWith({ bash: false, web: false, mode: 'agent' });
  const on = await sendWith({ bash: false, web: true, mode: 'agent' });

  assert.equal(off.get('mode'), 'agent');
  assert.equal(off.get('allow_web_search'), 'false');
  assert.equal(on.get('allow_web_search'), 'true');
});
