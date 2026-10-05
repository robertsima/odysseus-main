// Shared browser globals for node --test frontend tests.
//
// installDom() puts a happy-dom window on globalThis the way a browser does:
// `window === globalThis`, and document, localStorage, CustomEvent,
// MutationObserver, setTimeout and the rest are the window's. The real ES
// modules in static/js can then be imported unchanged.
//
// Static imports run before any code in the test file, so install the DOM
// first and load the module under test with `await import(...)`. Node runs
// each test file in its own process, which means one DOM per file and one
// copy of each imported module.
//
// fetch is not the network: until a test installs the fetch fake
// (fetchFake.mjs), every fetch rejects. Timers are happy-dom's, so restore()
// cancels whatever the imported modules left scheduled and the test process
// can exit.
//
//   const dom = installDom();
//   const { sanitizeAllowedHtml } = await import('../../../../static/js/markdown.js');
//   after(() => dom.restore());
import { GlobalWindow } from 'happy-dom';

// Node keeps these. fetch belongs to the fetch fake, which answers with
// Node's Response, so the fetch family stays Node's as well.
const KEEP_NODE_GLOBALS = new Set([
  'undefined', 'NaN', 'Infinity', 'global', 'globalThis', 'constructor',
  'console', 'process', 'Buffer',
  'fetch', 'Request', 'Response', 'Headers',
  'URL', 'URLSearchParams', 'AbortController', 'AbortSignal',
  'TextEncoder', 'TextDecoder', 'crypto', 'performance', 'structuredClone',
]);

// Window properties that point at the window itself point at globalThis.
const SELF_REFERENCES = ['window', 'self', 'top', 'parent', 'frames'];

async function noNetwork(input) {
  throw new TypeError(`fetch(${String(input?.url || input)}) in a frontend test: install the fetch fake`);
}

export function installDom({ url = 'http://localhost/', width = 1440, height = 900 } = {}) {
  const win = new GlobalWindow({ url, width, height });
  const saved = new Map();

  const define = (key, descriptor) => {
    if (!saved.has(key)) saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { ...descriptor, configurable: true });
  };

  for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(win))) {
    if (KEEP_NODE_GLOBALS.has(key) || SELF_REFERENCES.includes(key)) continue;
    if ('value' in descriptor && descriptor.value === globalThis[key]) continue;
    define(key, descriptor);
  }
  for (const key of SELF_REFERENCES) {
    define(key, { value: globalThis, writable: true, enumerable: true });
  }
  define('fetch', { value: noNetwork, writable: true, enumerable: true });

  return {
    window: win,
    document: win.document,
    async restore() {
      await win.happyDOM.abort();
      for (const [key, descriptor] of saved) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
      saved.clear();
      await win.happyDOM.close();
    },
  };
}

// Poll until `predicate()` is truthy, for work the code under test starts
// without returning a promise (an event handler that fetches, say).
export async function waitFor(predicate, { timeout = 2000, interval = 5, what = 'condition' } = {}) {
  const deadline = Date.now() + timeout;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out after ${timeout} ms waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}
