// A rail order saved before Today existed still shows Today first.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());
const nav = await import('../../../../static/js/navOrder.js');

test('an older full order gets Today at its default place, not last', () => {
  const full = nav.defaultNavOrder().filter((key) => key !== 'today');
  localStorage.setItem('odysseus-nav-order-v1', JSON.stringify({ value: full, updated_at: 1 }));
  const order = nav.readNavOrder();
  assert.equal(order.indexOf('today'), nav.defaultNavOrder().indexOf('today'));
  assert.notEqual(order.at(-1), 'today');
});

test('a short saved order keeps its own sequence and the rest follow', () => {
  localStorage.setItem('odysseus-nav-order-v1', JSON.stringify({ value: ['tasks', 'agents'], updated_at: 1 }));
  assert.deepEqual(nav.readNavOrder().slice(0, 2), ['tasks', 'agents']);
});
