// The serve request the Cookbook sends when it launches a model
// (static/js/cookbookRunning.js _launchServeTask): which platform the
// backend builds the runner for, and the venv activation it runs first.
import assert from 'node:assert/strict';
import { after, afterEach, test } from 'node:test';

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

const served = [];
fake.route('POST', '/api/model/serve', ({ body }) => {
  served.push(JSON.parse(body));
  return { ok: true, session_id: `serve-${served.length}` };
});
fake.route('GET', /^\/api\//, () => ({}));
fake.route('POST', /^\/api\//, () => ({}));

document.body.innerHTML = '<div id="toast"></div>';
const { _envState } = await import('../../../../static/js/cookbook.js');
const { _launchServeTask } = await import('../../../../static/js/cookbookRunning.js');

const FRESH_ENV = structuredClone({ ..._envState });
afterEach(() => {
  for (const key of Object.keys(_envState)) delete _envState[key];
  Object.assign(_envState, structuredClone(FRESH_ENV));
});

async function launchLocally(cmd = 'llama-server --model "m.gguf" --host 0.0.0.0 --port 8080') {
  const before = served.length;
  await _launchServeTask('m', 'org/m-GGUF', cmd, {}, '');
  await waitFor(() => served.length === before + 1, { what: 'the serve request' });
  return served.at(-1);
}

test('a local launch tells the backend the platform of the machine it runs on', async () => {
  // The backend host is Windows; a scan of a Linux server left its platform behind.
  _envState.hostPlatform = 'windows';
  _envState.platform = 'linux';
  const request = await launchLocally();
  assert.equal(request.remote_host, undefined);
  assert.equal(request.platform, 'windows');
});

test('a Windows venv path with a space and an apostrophe is activated as one quoted path', async () => {
  _envState.hostPlatform = 'windows';
  _envState.env = 'venv';
  _envState.envPath = "C:\\Users\\Jo O'Neil\\venvs\\llm";
  const request = await launchLocally();
  // PowerShell single quotes, with the embedded quote doubled.
  assert.equal(request.env_prefix, "& 'C:\\Users\\Jo O''Neil\\venvs\\llm\\Scripts\\Activate.ps1'");
});

test('two profiles on one host launch on the selected profile, with its port and platform', async () => {
  // A Linux host and a Windows VM behind it, reached on two SSH ports.
  const host = { name: 'gpu', host: 'gpu.lan', port: '22', platform: 'linux' };
  const vm = { name: 'gpu', host: 'gpu.lan', port: '2222', platform: 'windows' };
  _envState.servers = [host, vm];
  const { _serverKey } = await import('../../../../static/js/cookbook.js');
  const before = served.length;
  await _launchServeTask('m', 'org/m-GGUF', 'llama-server --model "m.gguf" --port 8080', {}, 'gpu.lan',
    { serverKey: _serverKey(vm) });
  await waitFor(() => served.length === before + 1, { what: 'the serve request' });

  const request = served.at(-1);
  assert.equal(request.remote_host, 'gpu.lan');
  assert.equal(String(request.ssh_port), '2222');
  assert.equal(request.platform, 'windows');
});
