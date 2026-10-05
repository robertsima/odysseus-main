// The session cost ledger in localStorage is shared by every open tab. A tab
// that read the ledger, then lost the race to a peer's write, must merge its
// run into what the peer wrote instead of overwriting it, or the peer's cost
// disappears from the session total. The write runs under a cross-tab lock, and
// a metrics payload is marked recorded only once the write actually ran.
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

// Two tabs are two copies of the module with one shared storage.
const tabA = await import('../../../../static/js/chatRenderer.js?tab=a');
const tabB = await import('../../../../static/js/chatRenderer.js?tab=b');
window.sessionModule = { getCurrentSessionId: () => 'session' };

// Web Locks: callbacks run one after another.
let lockTail = Promise.resolve();
Object.defineProperty(globalThis.navigator, 'locks', {
  configurable: true,
  value: {
    request(_name, callback) {
      const next = lockTail.then(callback);
      lockTail = next.catch(() => {});
      return next;
    },
  },
});

const RUNS_KEY = 'ody-session-cost-runs';
const metrics = (runId, inputTokens) => ({
  model: 'gpt-4o', input_tokens: inputTokens, output_tokens: 500,
  endpoint_cost_tracked: true, _costRecordId: runId,
});

test('a stale writer merges with a run its peer recorded in between', async () => {
  const costA = tabA.getModelCost('gpt-4o', 1000, 500);
  const costB = tabA.getModelCost('gpt-4o', 2000, 500);
  assert.ok(costA > 0 && costB > costA);

  // The first read of the ledger by tab A returns what it saw before the peer
  // wrote, and triggers the peer's write: tab A is now stale.
  const storage = {};
  let peerWrites = true;
  const realLocalStorage = globalThis.localStorage;
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem(key) {
        const stale = storage[key] ?? null;
        if (key === RUNS_KEY && peerWrites) {
          peerWrites = false;
          tabB.recordSessionMetricsCost(metrics('run-b', 2000), 'session');
        }
        return stale;
      },
      setItem(key, value) { storage[key] = value; },
      removeItem(key) { delete storage[key]; },
    },
  });
  try {
    const ours = metrics('run-a', 1000);
    tabA.recordSessionMetricsCost(ours, 'session');
    assert.deepEqual({ recorded: !!ours._costRecorded, pending: !!ours._costRecordPending },
      { recorded: false, pending: true }, 'recorded must not be claimed while the write waits on the lock');

    await new Promise((resolve) => setTimeout(resolve, 0));
    await lockTail;

    assert.deepEqual({ recorded: !!ours._costRecorded, pending: !!ours._costRecordPending },
      { recorded: true, pending: false });
    const runs = JSON.parse(storage[RUNS_KEY]).session;
    assert.deepEqual(Object.keys(runs).sort(), ['run-a', 'run-b']);
    assert.ok(Math.abs(runs['run-a'] - costA) < 1e-9 && Math.abs(runs['run-b'] - costB) < 1e-9);
  } finally {
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: realLocalStorage });
  }
});
