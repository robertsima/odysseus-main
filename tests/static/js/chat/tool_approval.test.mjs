// Answering a tool-approval card in static/js/chat.js. The click on "Allow
// once" travels as an opaque approval id plus one of the fixed decisions, in a
// send of its own: no composer text, no user bubble, no research run, and the
// draft in the composer stays where it was. The send goes through the chat
// form, not the shared send button's click handler (app.js reads an empty
// composer there as New chat or voice input).
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { DONE, jsonResponse, openChatPage } from './_page.mjs';

const page = await openChatPage({ mode: 'agent' });
after(() => page.close());

// What the send button's own click handler in app.js would do.
const sendButtonActions = [];
document.querySelector('.send-btn').addEventListener('click', () => sendButtonActions.push('click'));

const APPROVAL = {
  type: 'ask_user',
  data: {
    kind: 'tool_approval',
    approval_id: 'appr-1',
    question: 'Allow bash to run this command?',
    options: [
      { label: 'Allow once', value: 'approve' },
      { label: 'Allow for this task', value: 'approve_task' },
      { label: 'Deny', value: 'deny' },
    ],
    action: { tool: 'bash', content: 'rm -rf build' },
  },
};

function approvalCard() {
  return page.chatBox().querySelector('.ask-user-card[data-ask-user-kind="tool_approval"]');
}

function option(card, label) {
  return [...card.querySelectorAll('.ask-user-option')].find((el) => el.textContent.includes(label));
}

async function raiseApprovalCard() {
  page.reply([{ delta: 'I need to run a command.' }, APPROVAL, DONE]);
  await page.send('clean the build folder');
  assert.ok(approvalCard(), 'the stream raised an approval card');
}

beforeEach(() => {
  page.posts.length = 0;
  sendButtonActions.length = 0;
  document.getElementById('research-toggle').checked = false;
});

test('allowing once sends the approval id and decision with no message', async () => {
  await raiseApprovalCard();
  const bubbles = page.chatBox().querySelectorAll('.msg-user').length;
  page.reply([{ delta: 'Done.' }, DONE]);

  option(approvalCard(), 'Allow once').click();
  await page.waitFor(() => page.posts.length === 2, { what: 'the approval send' });

  const sent = page.posts[1];
  assert.equal(sent.tool_approval_id, 'appr-1');
  assert.equal(sent.tool_approval_decision, 'approve');
  assert.equal(sent.message, '');
  assert.equal(page.chatBox().querySelectorAll('.msg-user').length, bubbles, 'no user bubble for an approval');
  assert.deepEqual(sendButtonActions, [], "the send button's own action did not run");
});

test('allowing for the task sends the approve_task decision', async () => {
  await raiseApprovalCard();
  page.reply([DONE]);

  option(approvalCard(), 'Allow for this task').click();
  await page.waitFor(() => page.posts.length === 2, { what: 'the approval send' });

  assert.equal(page.posts[1].tool_approval_decision, 'approve_task');
});

test('a draft typed before answering stays in the composer', async () => {
  await raiseApprovalCard();
  page.reply([DONE]);
  page.composer().value = 'next question, half typed';

  option(approvalCard(), 'Deny').click();
  await page.waitFor(() => page.posts.length === 2, { what: 'the approval send' });

  assert.equal(page.posts[1].message, '');
  assert.equal(page.posts[1].tool_approval_decision, 'deny');
  await page.waitFor(() => !page.chat.hasActiveStream?.('s1'), { what: 'the reply to finish' });
  assert.equal(page.composer().value, 'next question, half typed');
});

test('an approval send never starts a research run', async () => {
  await raiseApprovalCard();
  page.reply([DONE]);
  document.getElementById('research-toggle').checked = true;

  option(approvalCard(), 'Allow once').click();
  await page.waitFor(() => page.posts.length === 2, { what: 'the approval send' });

  assert.equal(page.posts[1].use_research, undefined);
});

test('a refused approval comes back lapsed and leaves the mode alone', async () => {
  await raiseApprovalCard();
  const detail = 'This tool approval is invalid, expired, or belongs to another thread.';
  page.reply(jsonResponse({ detail }, { status: 409 }));

  option(approvalCard(), 'Allow once').click();
  await page.waitFor(() => page.posts.length === 2 && approvalCard()?.classList.contains('ask-user-card-lapsed'), {
    what: 'the card to come back lapsed',
  });

  assert.ok(approvalCard().textContent.includes(detail), "the card carries the server's reason");
  assert.ok(document.getElementById('mode-agent-btn').classList.contains('active'), 'Agent stays selected');
  assert.ok(!document.getElementById('mode-chat-btn').classList.contains('active'));
  assert.equal(JSON.parse(localStorage.getItem('agamemnon-toggles')).mode, 'agent');
});

test('a model that cannot use tools does switch the chat to Chat mode', async () => {
  // The counterpart of the test above: only this error flips the toggle.
  page.reply(jsonResponse({ detail: 'This model does not support tools' }, { status: 400 }));

  await page.send('list my files');

  assert.ok(document.getElementById('mode-chat-btn').classList.contains('active'));
  assert.equal(JSON.parse(localStorage.getItem('agamemnon-toggles')).mode, 'chat');
});
