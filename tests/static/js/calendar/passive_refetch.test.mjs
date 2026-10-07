// Calendar refetches nobody asked for in static/js/calendar.js: page boot, the
// chat's calendar-refresh event, and the tab becoming visible again. They are
// tagged X-Agamemnon-Poll. Without the tag the server's foreground gate reads a
// background refetch as the person at the keyboard and cancels the scheduled
// task that is running.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

// The fake does not record request headers, so wrap it.
const eventFetches = [];
const routed = globalThis.fetch;
globalThis.fetch = (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url, 'http://localhost/');
  if (url.pathname === '/api/calendar/events') eventFetches.push({ headers: init.headers || {} });
  return routed(input, init);
};

fake.route('GET', '/api/calendar/calendars', () => ({ calendars: [] }));
fake.route('GET', '/api/calendar/events', () => ({ events: [] }));

await import('../../../../static/js/calendar.js');

const poll = (fetches) => fetches.map((f) => f.headers['X-Agamemnon-Poll']);

test('the boot refetch of the current month is a poll', async () => {
  await waitFor(() => eventFetches.length >= 1, { what: 'the boot fetch' });
  assert.ok(poll(eventFetches).every((v) => v === '1'), 'boot fetches carry the poll header');
});

test('a calendar-refresh from the chat refetches as a poll', async () => {
  const before = eventFetches.length;
  window.dispatchEvent(new Event('calendar-refresh'));
  await waitFor(() => eventFetches.length > before, { what: 'the refresh fetch' });
  assert.deepEqual(poll(eventFetches.slice(before)), ['1']);
});

test('the tab becoming visible again refetches as a poll', async () => {
  const before = eventFetches.length;
  document.dispatchEvent(new Event('visibilitychange'));
  await waitFor(() => eventFetches.length > before, { what: 'the visibility fetch' });
  assert.deepEqual(poll(eventFetches.slice(before)), ['1']);
});
