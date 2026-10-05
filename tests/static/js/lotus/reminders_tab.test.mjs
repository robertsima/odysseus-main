// The Lotus Reminders tab is how a person schedules check-in nudges: it loads
// the saved schedule, saves edits through PUT /api/lotus/preferences, sends a
// test notification, snoozes, and lists what was sent. It shipped first as a
// stub that said reminders would come "in a later milestone", with none of the
// controls reaching the delivery endpoints.
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

let prefs = {
  timezone: 'Europe/Warsaw',
  reminder_enabled: true,
  reminder_times: ['08:30', '20:00'],
  reminder_weekdays: [0, 1, 2, 3, 4],
  quiet_start: '22:00',
  quiet_end: '07:00',
  snooze_minutes: 30,
  channel: 'email',
  min_hours_between: 2,
  skip_if_checked_in: true,
  message_style: 'plain',
  paused_until: null,
  insights_enabled: false,
  insights_frequency: 'weekly',
  insights_weekday: 6,
  insights_time: '09:00',
};
let sent = [];
fake.route('GET', '/api/lotus/overview', () => ({ total_checkins: 0, last_30_days: 0, last_checkin: null }));
fake.route('GET', '/api/lotus/preferences', () => prefs);
fake.route('PUT', '/api/lotus/preferences', ({ body }) => { prefs = JSON.parse(body); return prefs; });
fake.route('GET', '/api/lotus/notifications', () => ({ notifications: sent }));
fake.route('POST', '/api/lotus/preferences/test', () => {
  sent = [{ title: 'Time to check in', kind: 'test', channel: 'email', delivered: true, created_at: '2026-10-04T09:00:00Z', body: 'How are you?' }];
  return { delivered: true, channel: 'email' };
});
fake.route('POST', '/api/lotus/snooze', ({ body }) => ({ paused_until: '2026-10-04T10:00:00Z', minutes: JSON.parse(body).minutes }));

const { openLotus } = await import('../../../../static/js/lotus.js');
const byId = (id) => document.getElementById(id);
const times = () => [...document.querySelectorAll('#lotus-times .lotus-time')].map((input) => input.value);
const weekdays = () => [...document.querySelectorAll('#lotus-weekdays input:checked')].map((input) => Number(input.value));

test('the Reminders tab shows the saved schedule', async () => {
  openLotus();
  document.querySelector('[data-lotus-tab="reminders"]').click();
  await waitFor(() => times().length === 2, { what: 'the saved reminder times' });

  assert.equal(document.querySelector('[data-lotus-panel="reminders"]').hidden, false);
  assert.deepEqual(times(), ['08:30', '20:00']);
  assert.deepEqual(weekdays(), [0, 1, 2, 3, 4]);
  assert.equal(byId('lotus-reminder-enabled').checked, true);
  assert.equal(byId('lotus-quiet-start').value, '22:00');
  assert.equal(byId('lotus-channel').value, 'email');
  assert.equal(byId('lotus-timezone').value, 'Europe/Warsaw');
});

test('saving sends the edited schedule to the preferences endpoint', async () => {
  byId('lotus-add-time').click();
  document.querySelector('#lotus-weekdays input[value="5"]').checked = true;
  byId('lotus-channel').value = 'ntfy';
  byId('lotus-reminder-form').dispatchEvent(new Event('submit', { cancelable: true }));

  const saves = () => fake.calls.filter((c) => c.method === 'PUT' && c.url.pathname === '/api/lotus/preferences');
  await waitFor(() => saves().length, { what: 'the save' });
  const body = JSON.parse(saves()[0].body);
  assert.deepEqual(body.reminder_times, ['08:30', '20:00', '09:00']);
  assert.deepEqual(body.reminder_weekdays, [0, 1, 2, 3, 4, 5]);
  assert.equal(body.channel, 'ntfy');
  assert.equal(body.quiet_start, '22:00');
  assert.equal(body.quiet_end, '07:00');
  assert.equal(body.timezone, 'Europe/Warsaw');
});

test('a test notification is sent and then listed as sent', async () => {
  byId('lotus-test-notification').click();
  await waitFor(() => byId('lotus-notification-list').querySelector('.lotus-notification'), { what: 'the sent list' });

  assert.ok(fake.calls.some((c) => c.method === 'POST' && c.url.pathname === '/api/lotus/preferences/test'));
  assert.equal(byId('lotus-message').textContent, 'Test notification sent via email.');
  assert.match(byId('lotus-notification-list').textContent, /Time to check in/);
});

test('snoozing posts the chosen minutes and shows until when', async () => {
  byId('lotus-snooze-minutes').value = '60';
  byId('lotus-snooze').click();
  await waitFor(() => !byId('lotus-clear-snooze').hidden, { what: 'the snoozed state' });

  const snooze = fake.calls.find((c) => c.method === 'POST' && c.url.pathname === '/api/lotus/snooze');
  assert.deepEqual(JSON.parse(snooze.body), { minutes: 60 });
  assert.match(byId('lotus-pause-state').textContent, /^Snoozed until /);
});
