// The approval intercept in static/js/chatStream.js. Approving a tool clicks
// the shared send button from code; with an empty composer that button means
// New chat or Record voice, so the intercept turns the programmatic click into
// a form submit. A fixed 60 s timer used to disarm it, and an approval whose
// click came later fell through to New chat (2026-10-02).
import assert from 'node:assert/strict';
import { after, beforeEach, mock, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
after(async () => {
  mock.timers.reset();
  fake.restore();
  await dom.restore();
});

await import('../../../../static/js/chatStream.js');

let submits = 0;

beforeEach(() => {
  document.body.innerHTML = '<form id="chat-form"><button type="button" class="send-btn">Send</button></form>';
  submits = 0;
  document.getElementById('chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submits += 1;
  });
});

const sendButton = () => document.querySelector('.send-btn');

test('the approval click submits the form even when it comes minutes later', () => {
  document.dispatchEvent(new CustomEvent('odysseus:tool-approval'));

  mock.timers.tick(10 * 60 * 1000);
  sendButton().click();

  assert.equal(submits, 1);
});

test('a cancelled approval leaves the send button to its normal job', () => {
  document.dispatchEvent(new CustomEvent('odysseus:tool-approval'));
  document.dispatchEvent(new CustomEvent('odysseus:tool-approval-cancel'));

  sendButton().click();

  assert.equal(submits, 0);
});
