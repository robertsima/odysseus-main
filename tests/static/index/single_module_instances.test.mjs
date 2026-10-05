// The page loads each chat module once. index.html and the modules import
// app.js, chat.js, chatRenderer.js, chatStream.js, document.js and
// compare/stream.js with a ?v= cache-busting tag, and the browser keeps one
// module instance per distinct URL. An importer left on an old tag loads a
// second instance: two chatRenderer instances bind the ask_user keydown
// shortcut twice, two document.js instances split the open document's state,
// and chat.js paired with another chatStream.js instance loses the
// tool-approval click interceptor.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { after, test } from 'node:test';

import { installDom } from '../js/_support/dom.mjs';
import { installFetchFake } from '../js/_support/fetchFake.mjs';

const STATIC_DIR = new URL('../../../static/', import.meta.url);
const SINGLE_INSTANCE = [
  'app.js',
  'js/chat.js',
  'js/chatRenderer.js',
  'js/chatStream.js',
  'js/document.js',
  'js/compare/stream.js',
];

const loaded = new Set();
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    const result = nextResolve(specifier, context);
    loaded.add(result.url);
    return result;
  },
});

const dom = installDom();
const fake = installFetchFake();
// app.js reports activity with sendBeacon; keep it off happy-dom's network.
navigator.sendBeacon = () => true;
after(async () => {
  hooks.deregister();
  fake.restore();
  await dom.restore();
});

// The module URLs index.html asks for: module scripts and modulepreload links.
function pageEntries() {
  const html = readFileSync(new URL('index.html', STATIC_DIR), 'utf8');
  const page = new DOMParser().parseFromString(html, 'text/html');
  return [
    ...[...page.querySelectorAll('script[type="module"][src]')].map((el) => el.getAttribute('src')),
    ...[...page.querySelectorAll('link[rel="modulepreload"][href]')].map((el) => el.getAttribute('href')),
  ];
}

test('each chat module is loaded under one URL', async () => {
  for (const src of pageEntries()) {
    const url = new URL(src.replace(/^\/static\//, ''), STATIC_DIR);
    // Linking resolves the whole static graph before any module runs. With no
    // page markup some modules throw while running; the graph is complete.
    try { await import(url.href); } catch { /* see above */ }
  }

  const urlsOf = {};
  for (const href of loaded) {
    const url = new URL(href);
    if (!href.startsWith(STATIC_DIR.href)) continue;
    const path = url.pathname.slice(STATIC_DIR.pathname.length);
    (urlsOf[path] ||= new Set()).add(url.search);
  }

  for (const path of SINGLE_INSTANCE) {
    assert.ok(urlsOf[path], `${path} is part of the page`);
    assert.equal(urlsOf[path].size, 1, `${path} loads as ${[...urlsOf[path]].join(' and ')}`);
  }
});
