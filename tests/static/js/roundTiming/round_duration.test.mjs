// static/js/roundTiming.js: the wall-time badge on a completed agent round.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());
const { roundDurationLabel, showRoundDuration } = await import('../../../../static/js/roundTiming.js');

test('labels seconds, minutes and hours, and nothing for a missing or invalid time', () => {
  assert.deepEqual(
    [undefined, null, -1, Number.NaN, '5', 0, 2.34, 65, 3665].map(roundDurationLabel),
    ['', '', '', '', '', '0.0s', '2.3s', '1m 5s', '1h 1m'],
  );
});

test('a message with a round time gets one badge, updated in place', () => {
  const wrap = document.createElement('div');
  wrap.innerHTML = '<div class="role">Assistant</div>';
  showRoundDuration(wrap, 63.2);
  showRoundDuration(wrap, 5);
  const badges = wrap.querySelectorAll('.agent-round-duration');
  assert.equal(badges.length, 1);
  assert.equal(badges[0].textContent, 'Round · 5.0s');
  assert.equal(badges[0].parentElement.className, 'role');
});

test('a legacy message without a round time gets no badge', () => {
  const wrap = document.createElement('div');
  wrap.innerHTML = '<div class="role">Assistant</div>';
  showRoundDuration(wrap, undefined);
  assert.equal(wrap.querySelector('.agent-round-duration'), null);
});
