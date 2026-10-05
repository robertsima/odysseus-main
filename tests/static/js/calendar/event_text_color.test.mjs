// Event bars in the calendar carry the text color that reads on their own
// background (--cal-event-fg, from calendar/utils.js). Without it the CSS
// falls back to white, which cannot be read on a pastel event.
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

// happy-dom has no canvas, and the loading spinner draws on one. A context
// whose methods do nothing is enough here.
HTMLCanvasElement.prototype.getContext = () => new Proxy({}, {
  get: (target, key) => (key in target ? target[key] : () => {}),
});

// Multi-day all-day events in the current month, so the month view draws them
// as bars.
const now = new Date();
const day = (n) => `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(n).padStart(2, '0')}`;
const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
const EVENTS = [
  { uid: 'pastel', summary: 'Offsite', color: '#f2dfbd', dtstart: day(10), dtend: day(13), all_day: true },
  { uid: 'navy', summary: 'Conference', color: '#1f3552', dtstart: day(17), dtend: day(20), all_day: true },
  // Today, for the week view's all-day strip.
  { uid: 'today', summary: 'Holiday', color: '#b0d7f7', dtstart: ymd(now), dtend: ymd(tomorrow), all_day: true },
];
fake.route('GET', '/api/calendar/calendars', () => ({ calendars: [{ href: 'cal1', name: 'Home', color: '#3366cc' }] }));
fake.route('GET', '/api/calendar/events', () => ({ events: EVENTS }));

const calendar = await import('../../../../static/js/calendar.js');

function bar(uid) {
  return document.querySelector(`.cal-multiday[data-uid="${uid}"]`);
}

test('each multi-day bar gets text that reads on its own color', async () => {
  calendar.openCalendar();
  await waitFor(() => bar('pastel') && bar('navy'), { what: 'the event bars' });

  assert.equal(bar('pastel').style.getPropertyValue('--cal-event-fg'), '#111820');
  assert.equal(bar('navy').style.getPropertyValue('--cal-event-fg'), '#ffffff');
});

test("the week view's all-day events get the same text color", async () => {
  document.querySelector('.cal-view-btn[data-view="week"]').click();
  const event = () => document.querySelector('.cal-wk-allday-event[data-uid="today"]');
  await waitFor(event, { what: "today's all-day event in the week view" });

  assert.equal(event().style.getPropertyValue('--cal-event-fg'), '#111820');
});
