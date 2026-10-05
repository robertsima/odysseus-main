// The ask_user card in static/js/chatRenderer.js: an agent's multiple-choice
// question, or a tool-approval request. Digits 1-3 pick a question's option
// through the same path as a click, but never answer an approval card: a stray
// keypress must not grant a tool permission. Compare panes render the card in
// their own root and take the answer through a callback.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const { renderAskUserCard } = await import('../../../../static/js/chatRenderer.js');

const QUESTION = {
  question: 'Which format?',
  options: [{ label: 'Markdown' }, { label: 'PDF' }, { label: 'Word' }],
};
const APPROVAL = {
  kind: 'tool_approval',
  approval_id: 'appr-7',
  question: 'Allow bash to run this command?',
  options: [
    { label: 'Allow once', value: 'approve' },
    { label: 'Allow for this task', value: 'approve_task' },
    { label: 'Deny', value: 'deny' },
  ],
  action: { tool: 'bash', content: 'make deploy', document_id: 'doc-3' },
};

let submitted;
let approvalEvents;
document.addEventListener('odysseus:tool-approval', (event) => approvalEvents.push(event.detail));

beforeEach(() => {
  document.body.innerHTML = '<div id="chat-history"></div><div class="compare-pane"></div><input id="composer-like">';
  submitted = [];
  approvalEvents = [];
});

function render(payload, options = {}) {
  return renderAskUserCard(payload, {
    root: document.getElementById('chat-history'),
    onSubmit: (answer) => { submitted.push(answer); return options.accept; },
    scroll: false,
    focus: false,
    ...options,
  });
}

function press(key, init = {}, target = document.body) {
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }));
}

test('pressing 2 answers a question with its second option', () => {
  render(QUESTION);

  press('2');

  assert.deepEqual(submitted.map((a) => [a.kind, a.text]), [['answer', 'PDF']]);
});

test('a digit with a modifier, a held key or while typing does not answer', () => {
  render(QUESTION);

  press('1', { ctrlKey: true });
  press('1', { altKey: true });
  press('1', { metaKey: true });
  press('1', { shiftKey: true });
  press('1', { repeat: true });
  press('1', {}, document.getElementById('composer-like'));

  assert.equal(submitted.length, 0, 'no answer was submitted');
});

test('a digit never answers a tool-approval card', () => {
  const card = render(APPROVAL);

  press('1');
  press('2');

  // Lengths, not the answers: an answer holds the card element, and printing
  // a DOM node in a failure message takes a long time.
  assert.equal(submitted.length, 0, 'no answer was submitted');
  assert.equal(approvalEvents.length, 0, 'no approval event was sent');
  assert.ok(card.isConnected, 'the card is still waiting for a click');
});

test('a card renders into the root it is given and answers through the callback', () => {
  const pane = document.querySelector('.compare-pane');
  const card = render(QUESTION, { root: pane, accept: true });

  assert.ok(card.parentElement === pane, 'the card is in the pane');
  card.querySelectorAll('.ask-user-option')[2].click();

  assert.deepEqual(submitted.map((a) => [a.kind, a.text]), [['answer', 'Word']]);
  assert.equal(card.isConnected, false, 'an accepted answer removes the card');
});

test('an answer the callback refuses leaves the card in place', () => {
  const card = render(QUESTION, { accept: false });

  card.querySelector('.ask-user-option').click();

  assert.equal(submitted.length, 1);
  assert.ok(card.isConnected);
});

test('an approval click reports the approval id and the fixed decision', () => {
  const card = render(APPROVAL);

  [...card.querySelectorAll('.ask-user-option')][1].click();

  assert.equal(submitted.length, 1);
  const [answer] = submitted;
  assert.equal(answer.kind, 'tool_approval');
  assert.equal(answer.approval_id, 'appr-7');
  assert.equal(answer.decision, 'approve_task');
  assert.equal(answer.document_id, 'doc-3');
});

test('without a callback an approval click becomes an odysseus:tool-approval event', () => {
  const card = renderAskUserCard(APPROVAL, { scroll: false, focus: false });

  card.querySelector('.ask-user-option').click();

  assert.equal(approvalEvents.length, 1);
  assert.equal(approvalEvents[0].approval_id, 'appr-7');
  assert.equal(approvalEvents[0].decision, 'approve');
  assert.equal(card.isConnected, false);
});

test('a resolved approval from history is not rendered again', () => {
  assert.ok(render({ ...APPROVAL, resolved: true }) === null, 'no card is returned');
  assert.ok(document.querySelector('.ask-user-card') === null, 'no card is on the page');
});
