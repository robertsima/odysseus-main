// The login page (static/login.html) served by an app mounted under a path
// prefix, e.g. a reverse proxy that publishes Odysseus at /odysseus/. Every
// request the page makes and every redirect it issues must stay under that
// prefix; one that goes to /api/... or / reaches the proxy's other site and
// the user can never sign in.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../js/_support/dom.mjs';
import { installFetchFake } from '../js/_support/fetchFake.mjs';

const MOUNT = '/odysseus';
const dom = installDom({ url: `https://example.test${MOUNT}/login` });
const fake = installFetchFake();
after(() => { fake.restore(); return dom.restore(); });

const html = readFileSync(new URL('../../../static/login.html', import.meta.url), 'utf8');
const page = new DOMParser().parseFromString(html, 'text/html');

test('markup URLs are relative, so a mounted page resolves them under the mount', () => {
  const loginUrl = `https://example.test${MOUNT}/login`;
  const urls = [...page.querySelectorAll('link[href], script[src], img[src]')]
    .map((el) => el.getAttribute('href') || el.getAttribute('src'));
  assert.ok(urls.length > 0);
  for (const url of urls) {
    if (/^(https?:)?\/\//.test(url)) continue;
    assert.equal(url.startsWith('/'), false, `${url} is root-absolute and would leave the mount`);
    assert.ok(new URL(url, loginUrl).pathname.startsWith(`${MOUNT}/`), url);
  }
});

test('boot and sign-in stay under the mount, and signing in lands on the mounted root', async () => {
  document.body.innerHTML = page.body.innerHTML.replace(/<script[\s\S]*?<\/script>/g, '');
  const replaced = [];
  fake.route('GET', `${MOUNT}/api/version`, () => ({ version: '1.2.3' }));
  fake.route('GET', `${MOUNT}/api/auth/policy`, () => ({}));
  fake.route('GET', `${MOUNT}/api/auth/status`, () => ({ authenticated: false, configured: true }));
  fake.route('POST', `${MOUNT}/api/auth/login`, () => ({ ok: true }));
  for (const name of ['sessions', 'auth/features', 'auth/settings']) {
    fake.route('GET', `${MOUNT}/api/${name}`, () => ({}));
  }
  Object.defineProperty(window.location, 'replace', { value: (url) => replaced.push(String(url)), configurable: true });

  for (const script of page.querySelectorAll('script:not([type="module"])')) {
    (0, eval)(script.textContent);
  }
  await waitFor(() => fake.calls.length >= 3 && document.getElementById('version-label').textContent,
    { what: 'the page boot' });
  await new Promise((resolve) => setTimeout(resolve, 20));

  document.getElementById('username').value = 'alice';
  document.getElementById('password').value = 'correct horse';
  document.getElementById('authForm').dispatchEvent(new Event('submit', { cancelable: true }));
  await waitFor(() => replaced.length > 0, { what: 'the redirect after signing in' });

  assert.deepEqual(replaced, [`${MOUNT}/`]);
  assert.deepEqual(fake.unmatched, []);
  assert.ok(fake.calls.some((c) => c.method === 'POST' && c.url.pathname === `${MOUNT}/api/auth/login`));
  for (const call of fake.calls) assert.ok(call.url.pathname.startsWith(`${MOUNT}/api/`), call.url.pathname);
});
