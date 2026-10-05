// A saved agent reply renders one bubble per round (addMessage in
// static/js/chatRenderer.js). When a fallback answered part of the turn, each
// bubble must name the model and route that produced that round, from the
// saved per-round provenance, not the turn's final model.
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

function roundLabels() {
  // A role also carries the provider logo and, on the first round, a timestamp.
  return [...document.querySelectorAll('#chat-history .msg-ai .role')].map((role) => {
    const label = role.cloneNode(true);
    label.querySelectorAll('.role-timestamp, .role-provider-logo').forEach((el) => el.remove());
    return label.textContent.trim();
  });
}

test('each round is labelled with the model that answered it', () => {
  addMessage('assistant', 'Second round', 'gpt-4o', {
    requested_model: 'gpt-4o', model: 'local-fallback',
    requested_endpoint_id: 'ep-openai', requested_endpoint_label: 'OpenAI',
    endpoint_id: 'ep-local', endpoint_label: 'Local',
    round_texts: ['First round', 'Second round'],
    round_models: ['gpt-4o', 'local-fallback'],
    round_endpoint_ids: ['ep-openai', 'ep-local'],
    round_endpoint_labels: ['OpenAI', 'Local'],
  });
  assert.deepEqual(roundLabels(), ['gpt-4o', 'gpt-4o -> local-fallback']);
});

test('a round saved without a route keeps that, not the final round\'s route', () => {
  addMessage('assistant', 'Second round', 'gpt-4o', {
    requested_model: 'gpt-4o', model: 'gpt-4o',
    requested_endpoint_id: 'ep-primary', requested_endpoint_label: 'Primary',
    endpoint_id: 'ep-backup', endpoint_label: 'Backup',
    round_texts: ['First round', 'Second round'],
    round_models: ['gpt-4o', 'gpt-4o'],
    round_endpoint_ids: [null, 'ep-backup'],
    round_endpoint_labels: [null, 'Backup'],
  });
  assert.deepEqual(roundLabels(), ['gpt-4o', 'gpt-4o (Primary -> Backup)']);
});
