// Starting a new chat, in static/js/chatRenderer.js. "New chat" shows the
// welcome screen, and the draft typed in the previous chat must not ride
// along into the new one (#1343). Listeners on the composer (the send-button
// icon, autosize) hear about the change through an input event.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

document.body.innerHTML =
  '<div id="chat-container"><div id="welcome-screen" class="hidden"></div><div id="chat-history"></div></div>' +
  '<textarea id="message"></textarea>';
const { showWelcomeScreen } = await import('../../../../static/js/chatRenderer.js');

test('showing the welcome screen for a new chat empties the composer', () => {
  const composer = document.getElementById('message');
  composer.value = 'half-written question for the old chat';
  const inputs = [];
  composer.addEventListener('input', () => inputs.push(composer.value));

  showWelcomeScreen();

  assert.equal(composer.value, '');
  assert.deepEqual(inputs, ['']);
  assert.equal(document.getElementById('welcome-screen').classList.contains('hidden'), false);
});
