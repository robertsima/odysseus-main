// An agent turn that ended on an unanswered ask_user question (addMessage in
// static/js/chatRenderer.js). Reloading the chat must bring the choice card
// back from the saved tool events, and the user's next message, however it
// was sent, must take the card away.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
fake.route('GET', '/api/tools', () => ({ tools: [] }));
after(async () => { fake.restore(); await dom.restore(); });

document.body.innerHTML = '<div id="chat-history"></div>';
const { addMessage } = await import('../../../../static/js/chatRenderer.js');
const box = document.getElementById('chat-history');

beforeEach(() => { box.textContent = ''; });

function savedTurn(askUser) {
  return {
    model: 'gpt-4o',
    round_texts: ['Before I start:'],
    tool_events: [{ round: 1, tool: 'ask_user', command: 'ask_user', output: '', ask_user: askUser }],
  };
}

test('a reloaded chat shows its unanswered question as a choice card', () => {
  addMessage('user', 'Plan the migration');
  addMessage('assistant', 'Before I start:', 'gpt-4o', savedTurn({ question: 'Which database?', options: ['Postgres', 'SQLite'] }));

  const card = box.querySelector('.ask-user-card');
  assert.ok(card, 'the card is restored');
  assert.equal(card.querySelector('.ask-user-question').textContent, 'Which database?');
});

test('an answered question is not restored', () => {
  addMessage('assistant', 'Before I start:', 'gpt-4o', savedTurn({ question: 'Which database?', options: ['Postgres', 'SQLite'], resolved: true }));
  assert.equal(box.querySelectorAll('.ask-user-card').length, 0);
});

test('the next user message removes the card', () => {
  addMessage('assistant', 'Before I start:', 'gpt-4o', savedTurn({ question: 'Which database?', options: ['Postgres', 'SQLite'] }));
  assert.equal(box.querySelectorAll('.ask-user-card').length, 1);
  addMessage('user', 'Postgres, please');
  assert.equal(box.querySelectorAll('.ask-user-card').length, 0);
});
