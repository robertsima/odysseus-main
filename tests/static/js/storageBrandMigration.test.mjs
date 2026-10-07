import test from 'node:test';
import assert from 'node:assert/strict';
import {installStorageBrandMigration} from '../../../static/js/storageBrandMigration.js';

function fixture(entries) {
  class Storage {
    constructor() { this.values = new Map(entries); }
    getItem(k) { return this.values.get(String(k)) ?? null; }
    setItem(k, v) { this.values.set(String(k), String(v)); }
    removeItem(k) { this.values.delete(String(k)); }
  }
  const local = new Storage();
  const session = new Storage();
  installStorageBrandMigration(local, Storage.prototype);
  return {local, session};
}

test('upgrade preserves theme, model and extension preferences under canonical keys', () => {
  const {local} = fixture([['odysseus-theme', 'agamemnon'], ['odysseus.model', 'local'], ['unrelated', 'safe']]);
  assert.equal(local.getItem('agamemnon-theme'), 'agamemnon');
  assert.equal(local.values.get('agamemnon-theme'), 'agamemnon');
  assert.equal(local.getItem('agamemnon.model'), 'local');
  assert.equal(local.getItem('unrelated'), 'safe');
});

test('canonical value wins conflict; legacy callers write through; remove clears both', () => {
  const {local} = fixture([['odysseus-theme', 'old'], ['agamemnon-theme', 'new']]);
  assert.equal(local.getItem('odysseus-theme'), 'new');
  local.setItem('odysseus-theme', 'chosen');
  assert.equal(local.values.get('agamemnon-theme'), 'chosen');
  local.setItem('agamemnon-theme', 'again');
  assert.equal(local.values.get('odysseus-theme'), 'again');
  local.removeItem('odysseus-theme');
  assert.equal(local.values.size, 0);
});

test('session storage and unbranded keys are not rewritten', () => {
  const {local, session} = fixture([]);
  session.setItem('odysseus-theme', 'session');
  assert.equal(session.values.has('agamemnon-theme'), false);
  local.setItem('odysseuslike', 'value');
  assert.equal(local.values.has('agamemnonlike'), false);
});
