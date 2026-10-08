import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import vm from 'node:vm';
import { test } from 'node:test';

// A fresh module graph uses installed CacheStorage only. No HTTP-cache fallback.
test('storage initializes offline from the installed service-worker cache', async () => {
  const listeners = {}, cached = new Map();
  let offline = false;
  const key = request => typeof request === 'string' ? request : new URL(request.url).pathname;
  const cache = { put: async (request, response) => cached.set(key(request), response),
                  match: async request => cached.get(key(request)) };
  const scope = { console, URL, setTimeout,
    addEventListener: (type, fn) => { listeners[type] = fn; }, skipWaiting() {},
    caches: { open: async () => cache, match: cache.match },
    fetch: async request => {
      if (offline) throw new Error('offline, no HTTP cache');
      const path = key(request);
      let source = '';
      if (path.startsWith('/static/')) {
        try { source = readFileSync(new URL('../../../static/' + path.slice(8), import.meta.url), 'utf8'); }
        catch { return { ok: false }; }
      }
      return { ok: true, text: async () => source };
    } };
  scope.self = scope;
  vm.runInNewContext(readFileSync(new URL('../../../static/sw.js', import.meta.url), 'utf8'), scope);
  let installing;
  listeners.install({ waitUntil: promise => { installing = promise; } });
  await installing;
  offline = true;
  const modules = new Map();
  for (const path of ['/static/js/storage.js', '/static/js/storageBrandMigration.js']) {
    let response;
    listeners.fetch({ request: { url: 'http://localhost' + path, method: 'GET' },
                      respondWith: promise => { response = promise; } });
    const resource = await response;
    assert.ok(resource, path + ' must exist in installed CacheStorage');
    modules.set('offline:' + path, await resource.text());
  }
  const hook = registerHooks({
    resolve(specifier, context, next) {
      if (specifier === './storageBrandMigration.js' && context.parentURL === 'offline:/static/js/storage.js')
        return { url: 'offline:/static/js/storageBrandMigration.js', shortCircuit: true };
      if (modules.has(specifier)) return { url: specifier, shortCircuit: true };
      return next(specifier, context);
    },
    load(url, context, next) {
      if (modules.has(url)) return { format: 'module', source: modules.get(url), shortCircuit: true };
      return next(url, context);
    }
  });
  try {
    const storage = await import('offline:/static/js/storage.js');
    assert.equal(storage.KEYS.THEME, 'agamemnon-theme');
  } finally { hook.deregister(); }
});
