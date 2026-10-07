// A settings toggle shows its pref's default while the pref is unset.
//
// syncPrefToggle read an unset pref (null) as "on". "Publish learned skills"
// defaults off, so the switch showed on while learned skills stayed drafts.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
const { syncPrefToggle } = await import('../../../../static/js/memory.js');
after(() => { fake.restore(); dom.restore(); });

function toggle(id) {
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.id = id;
  document.body.appendChild(input);
  return input;
}

test('an unset default-off pref shows the switch off', async () => {
  fake.route('GET', '/api/prefs/off_pref', () => ({ key: 'off_pref', value: null }));
  const input = toggle('off-toggle');
  await syncPrefToggle('off-toggle', 'off_pref', 'on', 'off', false, false);
  assert.equal(input.checked, false);
});

test('an unset default-on pref still shows the switch on', async () => {
  fake.route('GET', '/api/prefs/on_pref', () => ({ key: 'on_pref', value: null }));
  const input = toggle('on-toggle');
  await syncPrefToggle('on-toggle', 'on_pref', 'on', 'off', false);
  assert.equal(input.checked, true);
});

test('a saved value wins over the default', async () => {
  fake.route('GET', '/api/prefs/set_pref', () => ({ key: 'set_pref', value: true }));
  const input = toggle('set-toggle');
  await syncPrefToggle('set-toggle', 'set_pref', 'on', 'off', false, false);
  assert.equal(input.checked, true);
});
