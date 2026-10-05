// The image editor opens offline. index.html never loads the editor: js/panels.js
// imports galleryEditor.js and its js/editor/ graph on first use, so those
// modules reach the service-worker cache only if static/sw.js precaches them at
// install, under the exact URL the import requests (query string included).
// A module missing from that list opens fine online and breaks the editor with
// no network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { after, test } from 'node:test';
import vm from 'node:vm';

import { installDom } from '../../static/js/_support/dom.mjs';

const STATIC_DIR = new URL('../../../static/', import.meta.url);

// Every module URL the editor panel's import resolves to.
const resolved = new Set();
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    const result = nextResolve(specifier, context);
    resolved.add(result.url);
    return result;
  },
});

const dom = installDom();
after(async () => {
  hooks.deregister();
  await dom.restore();
});

// The URL path the browser requests for a module, query string included.
function requestPath(fileUrl) {
  const url = new URL(fileUrl);
  return '/static/' + url.pathname.slice(STATIC_DIR.pathname.length) + url.search;
}

async function editorModules() {
  const { loadPanel } = await import('../../../static/js/panels.js');
  await loadPanel('editor');
  return [...resolved]
    .filter((url) => url.startsWith(STATIC_DIR.href))
    .map(requestPath)
    .filter((path) => path.startsWith('/static/js/galleryEditor.js') || path.startsWith('/static/js/editor/'));
}

// Run static/sw.js as the browser runs a service worker, fire its install
// event, and collect the URLs it stores in the cache.
async function precachedAtInstall() {
  const listeners = {};
  const stored = new Set();
  const cache = { put: async (url) => { stored.add(url); } };
  const scope = {
    addEventListener: (type, handler) => { listeners[type] = handler; },
    skipWaiting: () => {},
    caches: { open: async () => cache },
    fetch: async () => ({ ok: true }),
    console,
  };
  scope.self = scope;
  vm.runInNewContext(readFileSync(new URL('sw.js', STATIC_DIR), 'utf8'), scope);
  let installed;
  listeners.install({ waitUntil: (promise) => { installed = promise; } });
  await installed;
  return stored;
}

test('every module the editor panel loads is precached at service-worker install', async () => {
  const modules = await editorModules();
  assert.ok(modules.includes('/static/js/galleryEditor.js'), 'loadPanel("editor") loads galleryEditor.js');
  assert.ok(modules.some((path) => path.startsWith('/static/js/editor/')), 'and its js/editor/ graph');

  const precached = await precachedAtInstall();

  assert.deepEqual(modules.filter((path) => !precached.has(path)), []);
});
