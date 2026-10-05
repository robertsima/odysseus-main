// The Cookbook's Dependencies tab (static/js/cookbook.js). On a remote
// Windows server some engines cannot be installed; their rows say N/A.
// Everything else keeps an Install button.
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

const noop2d = new Proxy({}, { get: () => () => ({ addColorStop() {} }) });
HTMLCanvasElement.prototype.getContext = () => noop2d;

const WINBOX = { name: 'winbox', host: 'winbox', platform: 'windows' };
const PACKAGES = ['vllm', 'hf_transfer', 'rembg', 'gfpgan', 'diffusers', 'llama_cpp']
  .map((name) => ({ name, pip: name, installed: false, target: 'remote', kind: 'python' }));

fake.route('GET', '/api/cookbook/state', () => ({ env: { servers: [WINBOX], remoteHost: 'winbox', platform: 'windows' }, tasks: [] }));
fake.route('GET', '/api/cookbook/packages', () => ({ packages: PACKAGES }));
fake.route('GET', /^\/api\//, () => ({}));
fake.route('POST', /^\/api\//, () => ({}));

// The modal shell from static/index.html; open() renders into its body.
document.body.innerHTML = `
  <div id="toast"></div>
  <div id="cookbook-modal" class="modal hidden">
    <div class="modal-content"><div class="modal-header"><button id="close-cookbook-modal"></button></div>
    <div class="modal-body cookbook-body"></div></div>
  </div>`;
const cookbook = await import('../../../../static/js/cookbook.js');

function statusOf(name) {
  const row = document.querySelector(`#cookbook-deps-list [data-pkg-name="${name}"]`);
  if (row.querySelector('.cookbook-dep-na')) return 'n/a';
  if (row.querySelector('.cookbook-dep-install')) return 'install';
  return 'other';
}

test('on a remote Windows server, Diffusers can be installed and the Linux-only engines are N/A', async () => {
  await cookbook.open({ tab: 'Dependencies' });
  const picker = document.getElementById('hwfit-deps-server');
  picker.value = cookbook._serverKey(WINBOX);
  picker.dispatchEvent(new Event('change', { bubbles: true }));
  await waitFor(
    () => fake.calls.some((c) => c.url.pathname === '/api/cookbook/packages' && c.url.searchParams.get('host') === 'winbox')
      && document.querySelectorAll('#cookbook-deps-list [data-pkg-name]').length === PACKAGES.length,
    { what: 'the winbox package list', timeout: 5000 },
  );
  assert.equal(statusOf('diffusers'), 'install');
  assert.equal(statusOf('llama_cpp'), 'install');
  for (const name of ['vllm', 'hf_transfer', 'rembg', 'gfpgan']) assert.equal(statusOf(name), 'n/a', name);
});
