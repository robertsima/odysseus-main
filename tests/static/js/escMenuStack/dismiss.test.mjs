// The Escape-dismissal registry in static/js/escMenuStack.js. Dropdowns and
// popups that live outside the .modal system register a dismiss callback while
// open; the global Escape arbiter calls dismissTopMenu() to close the most
// recent one. The arbiter relies on: last opened closes first, exactly one menu
// per Escape, and a misbehaving menu never leaves the stack stuck.
import assert from 'node:assert/strict';
import test, { beforeEach } from 'node:test';

import {
  _openMenuCount,
  dismissTopMenu,
  registerMenuDismiss,
} from '../../../../static/js/escMenuStack.js';

beforeEach(() => {
  while (dismissTopMenu()) { /* empty the shared stack between tests */ }
});

test('with nothing open, Escape falls through to the modals', () => {
  assert.equal(dismissTopMenu(), false);
  assert.equal(_openMenuCount(), 0);
});

test('menus close last-opened first, one per Escape', () => {
  const order = [];
  registerMenuDismiss(() => order.push('A'));
  registerMenuDismiss(() => order.push('B'));

  assert.deepEqual([dismissTopMenu(), dismissTopMenu(), dismissTopMenu()], [true, true, false]);
  assert.deepEqual(order, ['B', 'A']);
  assert.equal(_openMenuCount(), 0);
});

test('a menu that closed itself is not dismissed again by Escape', () => {
  let fired = false;
  const unregister = registerMenuDismiss(() => { fired = true; });
  unregister();

  assert.equal(dismissTopMenu(), false);
  assert.equal(fired, false);
  assert.equal(_openMenuCount(), 0);
});

test('unregistering an older menu leaves the newer one on top', () => {
  const order = [];
  const unregisterA = registerMenuDismiss(() => order.push('A'));
  registerMenuDismiss(() => order.push('B'));
  unregisterA();

  dismissTopMenu();

  assert.deepEqual(order, ['B']);
  assert.equal(_openMenuCount(), 0);
});

test('a dismiss callback that throws still counts as handled and pops the stack', () => {
  registerMenuDismiss(() => { throw new Error('boom'); });

  assert.equal(dismissTopMenu(), true);
  assert.equal(_openMenuCount(), 0);
});

test('a registration that is not a function is ignored but still returns a callable', () => {
  const unregister = registerMenuDismiss(null);

  assert.equal(typeof unregister, 'function');
  assert.equal(_openMenuCount(), 0);
});
