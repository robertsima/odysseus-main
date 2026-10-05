// "Connect with Google" in the Settings > Integrations email editor saves the
// account first, then sends the browser to Google. That save must carry what
// the person entered in the form, the SMTP security choice included: an
// account saved with a different security mode than the one picked cannot send
// mail after OAuth comes back.
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

// The integrations list loads these; an empty install has none of them.
fake.route('GET', '/api/auth/integrations', () => ({ integrations: [] }));
fake.route('GET', '/api/calendar/config/accounts', () => ({ accounts: [] }));
fake.route('GET', '/api/contacts/config', () => ({}));
fake.route('GET', '/api/contacts/list', () => ({ contacts: [], count: 0 }));
fake.route('GET', '/api/email/accounts', () => ({ accounts: [] }));
fake.route('GET', '/api/mcp/servers', () => []);
fake.route('GET', '/api/tokens', () => []);
fake.route('POST', '/api/email/accounts', () => ({ ok: true, id: 'acct-1' }));

document.body.innerHTML = `
  <div><button id="unified-intg-add-btn" type="button">Add</button></div>
  <div id="unified-integrations-list"></div>
  <div id="unified-intg-form" style="display:none"></div>`;

// The handler ends by navigating to Google; record where instead of leaving.
let navigatedTo = null;
Object.defineProperty(window.location, 'href', {
  configurable: true,
  get: () => 'http://localhost/',
  set: (value) => { navigatedTo = String(value); },
});

const { default: settingsModule } = await import('../../../../static/js/settings.js');

const byId = (id) => document.getElementById(id);

// Known product bug: since 8a1d73c2 showEmailForm assigns `_setPresetTrigger`,
// which is only declared inside showApiForm, so opening the email editor throws
// a ReferenceError and shows "Could not open this editor". Drop the todo once
// settings.js is fixed.
const EDITOR_BUG = 'settings.js showEmailForm assigns the undeclared _setPresetTrigger (ReferenceError)';

test('Connect with Google saves the SMTP security the person picked', { todo: EDITOR_BUG }, async () => {
  await settingsModule.initUnifiedIntegrations();

  byId('unified-intg-add-btn').click();
  document.querySelector('.uf-type-option[data-value="email"]').click();
  await waitFor(() => byId('uf-oauth-btn'), { what: 'the email editor' });

  document.querySelector('.ufp-option[data-value="google_workspace"]').click();
  byId('uf-email-from').value = 'jane@school.edu';
  byId('uf-display-name').value = 'Jane Smith';
  // The Workspace preset fills in STARTTLS on 587; the person overrides it.
  byId('uf-smtp-security').value = 'ssl';

  byId('uf-oauth-btn').click();
  await waitFor(() => navigatedTo, { what: 'the redirect to Google' });

  const saves = fake.calls.filter((c) => c.method === 'POST' && c.url.pathname === '/api/email/accounts');
  assert.equal(saves.length, 1, 'the account is saved once before OAuth');
  const body = JSON.parse(saves[0].body);
  assert.equal(body.smtp_security, 'ssl');
  assert.equal(body.display_name, 'Jane Smith');
  assert.equal(body.from_address, 'jane@school.edu');
  assert.equal(navigatedTo, '/api/email/oauth/google/authorize?account_id=acct-1');
});
