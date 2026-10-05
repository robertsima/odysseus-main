// The Phalanx colours each soldier by the model family behind it. The model a
// run asked for wins over the execution source it ran through: a Claude model
// launched via the Codex runner is still an Anthropic soldier.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { resolveAgamemnonModelIdentity } from '../../../../static/js/agamemnonIdentity.js';

test('the explicit model decides the family before the execution source', () => {
  assert.equal(resolveAgamemnonModelIdentity('anthropic/claude-sonnet-5', 'codex').family, 'anthropic');
  assert.equal(resolveAgamemnonModelIdentity('google/gemini-3-pro', 'claude_code').family, 'google');
});

test('the source names the family only when the model does not', () => {
  assert.equal(resolveAgamemnonModelIdentity('', 'claude_code').family, 'anthropic');
  assert.equal(resolveAgamemnonModelIdentity(null, 'codex').family, 'openai');
});

test('unknown models get the neutral default, and each family has its own colour', () => {
  const unknown = resolveAgamemnonModelIdentity('acme/frobnicator-9', 'session');
  assert.equal(unknown.family, 'default');
  const colours = ['claude', 'gpt-5', 'gemini', 'mistral-large', 'qwen3', 'acme']
    .map((model) => resolveAgamemnonModelIdentity(model).color);
  assert.equal(new Set(colours).size, colours.length);
});
