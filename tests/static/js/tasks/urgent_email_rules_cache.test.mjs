// The email-triage task form saves its rules into the account settings
// (urgent_email_prompt). After that save, the shared /api/auth/settings
// snapshot in static/js/appConfig.js must be dropped, or the form and every
// other reader keep showing the old rules for the rest of the session.
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

// happy-dom has no canvas, and the Tasks window's loading spinner draws on
// one. A context whose methods do nothing is enough here.
HTMLCanvasElement.prototype.getContext = () => new Proxy({}, {
  get: (target, key) => (key in target ? target[key] : () => {}),
});

const TASK = {
  id: 't1',
  name: 'Triage inbox',
  task_type: 'action',
  action: 'check_email_urgency',
  trigger_type: 'schedule',
  schedule: 'every 30m',
  status: 'active',
  output_target: 'session',
};
const server = { settings: { urgent_email_prompt: 'old rules' } };
fake.route('GET', '/api/tasks', () => ({ tasks: [TASK] }));
fake.route('GET', '/api/tasks/meta/actions', () => ({
  actions: [{ name: 'check_email_urgency', description: 'Tag and flag urgent mail' }],
}));
fake.route('GET', '/api/tasks/meta/output-targets', () => ({ targets: [{ value: 'session', label: 'Session' }] }));
fake.route('PUT', '/api/tasks/t1', ({ body }) => ({ ...TASK, ...JSON.parse(body) }));
fake.route('GET', '/api/auth/settings', () => ({ ...server.settings }));
fake.route('POST', '/api/auth/settings', ({ body }) => {
  Object.assign(server.settings, JSON.parse(body));
  return { ...server.settings };
});

const { getSettings } = await import('../../../../static/js/appConfig.js');
const tasks = await import('../../../../static/js/tasks.js');

test('saved triage rules reach the next shared settings read', async () => {
  assert.equal((await getSettings()).urgent_email_prompt, 'old rules');

  tasks.openTasks('t1');
  await waitFor(() => document.querySelector('.task-detail-edit-btn'), { what: 'the task detail' });
  document.querySelector('.task-detail-edit-btn').click();
  await waitFor(() => document.getElementById('task-form-urgent-email-prompt')?.dataset.loaded,
    { what: 'the triage rules field' });
  document.getElementById('task-form-urgent-email-prompt').value = 'new rules';
  document.getElementById('task-form-save').click();
  await waitFor(() => fake.calls.some((c) => c.method === 'PUT'), { what: 'the task save' });

  assert.equal(server.settings.urgent_email_prompt, 'new rules');
  assert.equal((await getSettings()).urgent_email_prompt, 'new rules');
});
