// static/js/roundTiming.js: the combined wall-time badge on a completed turn.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());
const { durationLabel, totalTurnDuration, showTurnDuration } = await import('../../../../static/js/roundTiming.js');

test('labels seconds, minutes and hours, and nothing for a missing or invalid time', () => {
  assert.deepEqual(
    [undefined, null, -1, Number.NaN, '5', 0, 2.34, 65, 3665].map(durationLabel),
    ['', '', '', '', '', '0.0s', '2.3s', '1m 5s', '1h 1m'],
  );
});

test('a turn total sums completed rounds and ignores missing durations', () => {
  assert.equal(totalTurnDuration([63.24, 5]), 68.24);
  assert.equal(totalTurnDuration([63.24, null, 5]), 68.24);
  assert.equal(totalTurnDuration([null, undefined]), null);
  assert.equal(totalTurnDuration('legacy'), null);
});

test('a message with a turn time gets one badge, updated in place', () => {
  const wrap = document.createElement('div');
  wrap.innerHTML = '<div class="role">Assistant</div>';
  showTurnDuration(wrap, 68.24);
  showTurnDuration(wrap, 70);
  const badges = wrap.querySelectorAll('.agent-turn-duration');
  assert.equal(badges.length, 1);
  assert.equal(badges[0].textContent, 'Turn · 1m 10s');
  assert.equal(badges[0].parentElement.className, 'role');
});

test('a legacy message without a turn time gets no badge', () => {
  const wrap = document.createElement('div');
  wrap.innerHTML = '<div class="role">Assistant</div>';
  showTurnDuration(wrap, undefined);
  assert.equal(wrap.querySelector('.agent-turn-duration'), null);
});
