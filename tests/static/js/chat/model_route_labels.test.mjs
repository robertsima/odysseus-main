// Which model answered, per round, in a live agent reply (static/js/chat.js).
// An agent turn renders one bubble per round. When round 2 falls back to
// another model, or the provider reports an alias, or the final metrics name
// the model of the last round, that label belongs on round 2's bubble.
// Writing it on the first bubble claims the first round was answered by a
// model that never saw it, and hides that round 2 switched.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { DONE, openChatPage } from './_page.mjs';

const page = await openChatPage({ model: 'model-a', mode: 'agent' });
after(() => page.close());

const roleLabels = () => page.aiMessages().slice(-2).map((m) => m.querySelector('.role').textContent);

test('a fallback in round 2 relabels round 2, not round 1', async () => {
  page.reply([
    { delta: 'Checking the logs.' },
    { type: 'agent_step', round: 2 },
    { type: 'fallback', round: 2, selected_model: 'model-a', answered_by: 'backup-model', reason: '429' },
    { delta: 'The logs are clean.' },
    DONE,
  ]);

  await page.send('check the logs');

  const [round1, round2] = roleLabels();
  assert.ok(!round1.includes('backup-model'), `round 1 reads "${round1}"`);
  assert.ok(round2.includes('backup-model'), `round 2 reads "${round2}"`);
});

test("a provider's alias for round 2 relabels round 2, not round 1", async () => {
  page.reply([
    { delta: 'Reading the file.' },
    { type: 'agent_step', round: 2 },
    { type: 'model_actual', round: 2, requested_model: 'model-a', model: 'alias-of-b' },
    { delta: 'It has two sections.' },
    DONE,
  ]);

  await page.send('read the file');

  const [round1, round2] = roleLabels();
  assert.ok(!round1.includes('alias-of-b'), `round 1 reads "${round1}"`);
  assert.ok(round2.includes('alias-of-b'), `round 2 reads "${round2}"`);
});

test("the final metrics label the last round with that round's model", async () => {
  page.reply([
    { delta: 'Step one.' },
    { type: 'agent_step', round: 2 },
    { delta: 'Step two.' },
    { type: 'metrics', data: { requested_model: 'model-a', model: 'model-c', round_models: ['model-a', 'model-c'] } },
    DONE,
  ]);

  await page.send('do two steps');

  const [round1, round2] = roleLabels();
  assert.ok(!round1.includes('model-c'), `round 1 reads "${round1}"`);
  assert.ok(round2.includes('model-c'), `round 2 reads "${round2}"`);
});
