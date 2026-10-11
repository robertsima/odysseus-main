// A saved agent reply under a persona renders one bubble per round, and every
// bubble carries the persona's name (addMessage in static/js/chatRenderer.js).
// On 2026-10-11 a loadout chat showed its persona on single-bubble replies and
// the model's name on every bubble of a multi-round reply, so one turn read as
// two speakers. The route still belongs on the label's tooltip.
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

beforeEach(() => { document.getElementById('chat-history').textContent = ''; });

function roles() {
  return [...document.querySelectorAll('#chat-history .msg-ai .role')].map((role) => {
    const label = role.cloneNode(true);
    label.querySelectorAll('.role-timestamp, .role-provider-logo').forEach((el) => el.remove());
    return { text: label.textContent.trim(), title: role.title };
  });
}

test('every round of a persona reply is labelled with the persona, the model on the tooltip', () => {
  addMessage('assistant', 'Final round', 'gpt-6-luna', {
    model: 'gpt-6-luna', character_name: 'Dono Fuel PO',
    round_texts: ['Captured that in the note.', 'Final round'],
    round_models: ['gpt-6-luna', 'gpt-6-luna'],
    tool_events: [{ tool: 'manage_notes', round: 1, exit_code: 0 }],
  });
  const seen = roles();
  assert.deepEqual(seen.map((r) => r.text), ['Dono Fuel PO', 'Dono Fuel PO']);
  assert.ok(seen.every((r) => r.title.includes('gpt-6-luna')), JSON.stringify(seen));
});

test('a multi-round reply without a persona keeps the model labels', () => {
  addMessage('assistant', 'Final round', 'gpt-6-luna', {
    model: 'gpt-6-luna',
    round_texts: ['First', 'Final round'],
    round_models: ['gpt-6-luna', 'gpt-6-luna'],
  });
  assert.deepEqual(roles().map((r) => r.text), ['gpt-6-luna', 'gpt-6-luna']);
});
