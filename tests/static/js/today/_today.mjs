// The Today view (static/js/today.js) on a node page: its window markup, the
// routes it calls, and a fixed /api/today payload. Each test file gets one
// view (node runs every file in its own process).
//
//   const view = await openToday({ today: [...], next: [...] });
//   await view.show();
import { after } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

export { waitFor };

const MARKUP = `
  <div id="today-modal" class="modal hidden" hidden tabindex="-1">
    <div class="modal-content">
      <div class="modal-header"><h3 id="today-title">Today</h3><button id="close-today" type="button">x</button></div>
      <div id="today-body"></div>
    </div>
  </div>`;

export function item(key, overrides = {}) {
  return {
    key, title: `Title of ${key}`, why: `Why ${key}`, source: 'todoist', due: null, state: null, blocker: null,
    actions: ['start', 'defer', 'wait', 'done', 'open'], open_url: 'https://app.todoist.com/app/task/1', ...overrides,
  };
}

export async function openToday({ today = [], next = [], degraded = [], onAction = null, onCapture = null } = {}) {
  const dom = installDom();
  const fake = installFetchFake();
  after(async () => {
    fake.restore();
    await dom.restore();
  });
  document.body.innerHTML = MARKUP;
  const payload = { today, next, degraded, generated_at: '2026-10-10T10:00:00Z' };
  const view = { payload, opened: [], selected: [], clicked: [] };
  fake.route('GET', '/api/today', () => view.payload);
  fake.route('POST', '/api/today/action', ({ body }) => (onAction ? onAction(JSON.parse(body)) : { ok: true }));
  fake.route('POST', '/api/capture', ({ body }) => (onCapture ? onCapture(JSON.parse(body)) : { status: 'failed', error: 'no handler' }));
  window.sessionModule = { selectSession: (sid) => view.selected.push(sid), getCurrentSessionId: () => null };
  window.open = (url) => view.opened.push(url);

  const { default: todayModule } = await import('../../../../static/js/today.js');
  const root = document.getElementById('today-modal');
  Object.assign(view, {
    fake, today: todayModule, root,
    body: () => document.getElementById('today-body'),
    rows: () => [...root.querySelectorAll('.today-item')],
    keys: () => [...root.querySelectorAll('.today-item')].map((li) => li.dataset.key),
    row: (key) => root.querySelector(`.today-item[data-key="${key}"]`),
    buttons: (key) => [...view.row(key).querySelectorAll('[data-today-action]')].map((b) => b.dataset.todayAction),
    posts: (path) => fake.calls.filter((c) => c.method === 'POST' && c.url.pathname === path).map((c) => JSON.parse(c.body)),
    reads: () => fake.calls.filter((c) => c.method === 'GET' && c.url.pathname === '/api/today').length,
    click(el) { el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })); },
    async show() {
      todayModule.open();
      await waitFor(() => root.querySelector('.today-lists') && !root.querySelector('.today-loading'), { what: 'the lists' });
    },
    type(text) {
      const input = root.querySelector('#today-capture-input');
      input.value = text;
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    },
    submit() {
      root.querySelector('#today-capture').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
    },
  });
  return view;
}
