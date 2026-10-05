// Shared setup for the theme.js account-sync tests.
//
// theme.js reconciles this browser's theme with the account's as soon as it
// is imported, so each test file seeds localStorage and the fake /api/prefs
// first, then imports the module. Node runs each file in its own process,
// which gives every file a fresh boot.
import { readFileSync } from 'node:fs';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

export const THEME_KEY = 'odysseus-theme';

const INDEX_HTML = new URL('../../../../static/index.html', import.meta.url);

// theme.js applies a theme from initThemeUI(), which needs the theme window
// that index.html ships (it stops early without the theme grid). Mount that
// window from the real page, beside the theme-color meta it keeps in step
// with the background.
function mountThemeWindow() {
  const page = new DOMParser().parseFromString(readFileSync(INDEX_HTML, 'utf8'), 'text/html');
  const modal = page.getElementById('theme-modal');
  if (!modal) throw new Error('static/index.html has no #theme-modal');
  document.head.innerHTML = '<meta name="theme-color" content="#000000">';
  document.body.innerHTML = '';
  document.body.appendChild(document.importNode(modal, true));
}

// `account` maps a pref key to what the server holds for the signed-in user
// (an envelope `{ value, updated_at }`, or a bare legacy value). `liveUser`
// answers /api/auth/status. `hold` makes GET /api/prefs/* wait until
// `release()` is called. PUT bodies are collected per key in `puts`.
export function bootTheme({ local = {}, account = {}, liveUser = null, hold = false } = {}) {
  const dom = installDom();
  mountThemeWindow();
  for (const [key, value] of Object.entries(local)) {
    localStorage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value));
  }

  const fake = installFetchFake();
  const puts = [];
  let release = () => {};
  const gate = hold ? new Promise((resolve) => { release = resolve; }) : Promise.resolve();
  fake.route('GET', '/api/auth/status', () => ({ username: liveUser }));
  fake.route('GET', /^\/api\/prefs\/[^/]+$/, async ({ url }) => {
    await gate;
    const key = decodeURIComponent(url.pathname.split('/').pop());
    return { key, value: key in account ? account[key] : null };
  });
  fake.route('PUT', /^\/api\/prefs\/[^/]+$/, ({ url, body }) => {
    const key = decodeURIComponent(url.pathname.split('/').pop());
    const { value } = JSON.parse(body);
    puts.push({ key, ...value });
    return { key, value };
  });

  return {
    dom,
    fake,
    puts,
    release: () => release(),
    savedTheme: () => JSON.parse(localStorage.getItem(THEME_KEY) || 'null'),
    cssVar: (name) => document.documentElement.style.getPropertyValue(name),
    async restore() {
      fake.restore();
      await dom.restore();
    },
  };
}

// writePref() batches writes for 200 ms before it sends the PUT.
export const settled = () => new Promise((resolve) => setTimeout(resolve, 400));
