// The What-Fits hardware scan (static/js/cookbook-hwfit.js _hwfitFetch)
// probes the selected server. Two profiles can share a host and differ in
// SSH port; the scan must reach the selected one.
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

fake.route('GET', '/api/model/cached', () => ({ models: [] }));
fake.route('GET', '/api/hwfit/models', () => ({ system: { backend: 'cuda' }, models: [] }));
fake.route('GET', /^\/api\//, () => ({}));

const { _envState, _serverKey } = await import('../../../../static/js/cookbook.js');
const { _hwfitFetch } = await import('../../../../static/js/cookbook-hwfit.js');

test('the hardware scan reaches the selected profile, not the first server on that host', async () => {
  const host = { name: 'gpu', host: 'gpu.lan', port: '22', platform: 'linux' };
  const vm = { name: 'gpu-vm', host: 'gpu.lan', port: '2222', platform: 'linux' };
  _envState.servers = [host, vm];
  _envState.remoteHost = 'gpu.lan';
  _envState.remoteServerKey = _serverKey(vm);
  document.body.innerHTML = '<div id="hwfit-list"></div><div id="hwfit-hw"></div>';

  await _hwfitFetch(true);
  await waitFor(() => fake.calls.some((c) => c.url.pathname === '/api/model/cached'), { what: 'the cached-model scan' });

  for (const path of ['/api/hwfit/models', '/api/model/cached']) {
    const call = fake.calls.find((c) => c.url.pathname === path);
    assert.equal(call.url.searchParams.get('host'), 'gpu.lan', path);
    assert.equal(call.url.searchParams.get('ssh_port'), '2222', path);
  }
});
