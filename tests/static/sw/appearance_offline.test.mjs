import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

test('first paint and title placement dependencies install for an offline theme boot', async () => {
  const listeners = {}, cached = new Map();
  let offline = false;
  const key = r => typeof r === 'string' ? r : new URL(r.url).pathname;
  const cache = {put: async (r, response) => cached.set(key(r), response),
                 match: async r => cached.get(key(r))};
  const scope = {console, URL, setTimeout, caches: {open: async () => cache, match: cache.match},
    addEventListener: (type, fn) => { listeners[type] = fn; }, skipWaiting() {},
    fetch: async r => {
      if (offline) throw new Error('offline; no HTTP cache');
      let source = '';
      if (key(r).startsWith('/static/')) {
        try { source = readFileSync(new URL('../../../' + key(r).slice(1), import.meta.url), 'utf8'); }
        catch { return {ok: false}; }
      }
      return {ok: true, text: async () => source};
    }};
  scope.self = scope;
  vm.runInNewContext(readFileSync(new URL('../../../static/sw.js', import.meta.url), 'utf8'), scope);
  let installing;
  listeners.install({waitUntil: p => { installing = p; }});
  await installing;
  offline = true;
  const boot = {};
  for (const path of ['/static/js/appearancePreferences.js', '/static/js/chatHeader.js']) {
    let result;
    listeners.fetch({request: {url: 'http://localhost' + path, method: 'GET'},
                     respondWith: p => { result = p; }});
    const response = await result;
    assert.ok(response, path + ' must be available without a prior online page visit');
    if (path.endsWith('appearancePreferences.js')) vm.runInNewContext(await response.text(), boot);
  }
  assert.match(boot.AgamemnonAppearance.fontFamily('serif'), /Georgia/);
  assert.equal(boot.AgamemnonAppearance.pageStyle({value: 'agamemnon'}, {name: 'paper'}), 'agamemnon');
});
