// Settings > Reminders offers email as a channel only for accounts that can
// send. A Google OAuth account sends with its token and stores no SMTP
// password, so a password-only check left Workspace and .edu users without
// the email channel even though the reminder sender supports OAuth SMTP.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';
import { mountSettingsModal } from './_modal.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const OAUTH = {
  id: 'oauth-1', name: 'School', smtp_host: 'smtp.gmail.com', smtp_user: 'jane@school.edu',
  has_smtp_password: false, oauth_provider: 'google',
};
const NO_PASSWORD = {
  id: 'half-1', name: 'Half set up', smtp_host: 'smtp.example.com', smtp_user: 'me@example.com',
  has_smtp_password: false, oauth_provider: null,
};
const PASSWORD = {
  id: 'pw-1', name: 'Personal', smtp_host: 'smtp.example.com', smtp_user: 'me@example.com',
  has_smtp_password: true, oauth_provider: null,
};
let accounts = [OAUTH, NO_PASSWORD];
fake.route('GET', '/api/email/accounts', () => ({ accounts }));
fake.route('GET', '/api/auth/settings', () => ({ reminder_channel: 'email' }));

mountSettingsModal();
const { default: settingsModule } = await import('../../../../static/js/settings.js');
const byId = (id) => document.getElementById(id);
const senderIds = () => [...byId('set-reminder-email-account').options].map((o) => o.value);

test('a Google OAuth account without an SMTP password can send reminders', async () => {
  settingsModule.open('reminders');
  const channel = byId('set-reminder-channel');
  await waitFor(() => channel.value === 'email', { what: 'the saved email channel' });

  assert.equal(byId('set-reminder-channel-email-opt').disabled, false);
  assert.deepEqual(senderIds(), ['oauth-1']);
});

test('the sender list refreshed after a Connections change keeps OAuth accounts', async () => {
  accounts = [OAUTH, NO_PASSWORD, PASSWORD];
  window.dispatchEvent(new CustomEvent('odysseus-integrations-changed'));
  await waitFor(() => senderIds().length === 2, { what: 'the refreshed sender list' });

  assert.deepEqual(senderIds(), ['oauth-1', 'pw-1']);
  assert.equal(byId('set-reminder-channel').value, 'email');
});
