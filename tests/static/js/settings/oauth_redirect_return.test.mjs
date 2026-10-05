// Google OAuth sends the browser back to the app with ?email_oauth_success (or
// _error) in the URL. settings.js must then open Settings on Integrations and
// say how the sign-in went. The handler once waited for a window-level alias
// that nothing set any more, so the person came back to a page that showed
// nothing at all.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';
import { mountSettingsModal } from './_modal.mjs';

const dom = installDom({ url: 'http://localhost/?email_oauth_error=needs_https#chat' });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const modal = mountSettingsModal();
await import('../../../../static/js/settings.js');

test('returning from Google opens Settings on Integrations with the result', async () => {
  await waitFor(() => !modal.classList.contains('hidden'), { what: 'Settings to open' });

  const integrations = modal.querySelector('[data-settings-panel="integrations"]');
  assert.equal(integrations.classList.contains('hidden'), false);
  assert.equal(modal.querySelector('[data-settings-tab="integrations"]').classList.contains('active'), true);

  const banner = [...document.body.children].find((node) => node.textContent.startsWith('Google sign-in failed'));
  assert.ok(banner, 'a banner reports the failed sign-in');
  assert.match(banner.textContent, /only redirects back to an https address/);

  // The result parameters are stripped so a reload does not report it again.
  assert.equal(window.location.search, '');
  assert.equal(window.location.hash, '#chat');
});
