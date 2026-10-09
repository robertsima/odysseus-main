// The agent strip's progress line says where a worker is and when it is
// waiting (static/js/workbench.js fmtProgress). "round 74 · read_file" did not
// say which file, and a worker waiting to resume after a provider error looked
// exactly like a stuck one (2026-10-08).
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

const { fmtProgress } = await import('../../../../static/js/workbench.js');

test('the line names what the current tool works on', () => {
  const line = fmtProgress(
    { round: 12, current_tool: 'read_file', current_target: 'static/js/theme.js' },
    { startedAt: 1000, now: 1030 },
  );
  assert.deepEqual(line, { text: 'round 12 · read_file static/js/theme.js', warn: false });
});

test('a worker waiting to resume says so', () => {
  const line = fmtProgress(
    { round: 74, waiting: 'Upstream model error; resuming in 30s (1/2)' },
    { startedAt: 1000, now: 1030 },
  );
  assert.deepEqual(line, { text: 'round 74 · Upstream model error; resuming in 30s (1/2)', warn: true });
});
