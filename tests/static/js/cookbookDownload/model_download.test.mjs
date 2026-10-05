// Starting a model download from the Cookbook
// (static/js/cookbookDownload.js _runModelDownload): the server it goes to,
// the venv it activates there, and the error a failed start leaves on screen.
import assert from 'node:assert/strict';
import { after, afterEach, beforeEach, test } from 'node:test';

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

const requests = [];
let answer = () => ({ ok: true, session_id: `dl-${requests.length}` });
fake.route('POST', '/api/model/download', ({ body }) => {
  requests.push(JSON.parse(body));
  return answer();
});
const probes = [];
fake.route('POST', '/api/shell/exec', ({ body }) => {
  probes.push(JSON.parse(body).command);
  return { exit_code: 1, stdout: '' };
});
fake.route('GET', /^\/api\//, () => ({}));
fake.route('POST', /^\/api\//, () => ({}));

const { _envState, _serverKey } = await import('../../../../static/js/cookbook.js');
const { _runModelDownload } = await import('../../../../static/js/cookbookDownload.js');

// ui.js keeps the first #toast it finds, so the page keeps one for the file.
document.body.innerHTML = '<div id="toast"></div>';

const FRESH_ENV = structuredClone({ ..._envState });
beforeEach(() => {
  document.getElementById('hwfit-server-select')?.remove();
  document.getElementById('toast').textContent = '';
  localStorage.clear();
  answer = () => ({ ok: true, session_id: `dl-${requests.length}` });
});
afterEach(() => {
  for (const key of Object.keys(_envState)) delete _envState[key];
  Object.assign(_envState, structuredClone(FRESH_ENV));
});

const MODEL = { name: 'org/model-7b', required_gb: 1 };
const panel = () => document.createElement('div');

function headerPicker(servers, selected) {
  const select = document.createElement('select');
  select.id = 'hwfit-server-select';
  for (const value of ['local', ...servers.map(_serverKey)]) {
    const option = document.createElement('option');
    option.value = value;
    select.append(option);
  }
  select.value = selected;
  document.body.append(select);
}

async function download(hostOverride) {
  const before = requests.length;
  await _runModelDownload(panel(), MODEL, 'vllm', hostOverride);
  assert.equal(requests.length, before + 1, 'one download request');
  return requests.at(-1);
}

const toast = () => document.getElementById('toast');

for (const [name, fail] of [
  ['an HTTP error', () => new Response('{}', { status: 500 })],
  ['a refusal from the backend', () => ({ ok: false, error: 'tmux is required on the target server' })],
  ['a network failure', () => { throw new TypeError('Failed to fetch'); }],
]) {
  test(`a download that fails to start on ${name} keeps its error on screen`, async () => {
    answer = fail;
    await _runModelDownload(panel(), MODEL, 'vllm', '');
    assert.match(toast().textContent, /^Download failed:/);
    // The default toast is gone after 1.2 s; this one must still be readable.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    assert.ok(toast().classList.contains('show'), 'the error toast is still showing');
  });
}

test('a Windows venv path with a space and an apostrophe is activated as one quoted path', async () => {
  const winbox = { host: 'winbox', platform: 'windows', env: 'venv', envPath: "C:\\Users\\Jo O'Neil\\venv" };
  _envState.servers = [winbox];
  const request = await download('winbox');
  assert.equal(request.env_prefix, "& 'C:\\Users\\Jo O''Neil\\venv\\Scripts\\Activate.ps1'");
});

test('a download goes to the profile picked in the header, with its SSH port', async () => {
  const host = { name: 'gpu', host: 'gpu.lan', port: '22' };
  const vm = { name: 'gpu-vm', host: 'gpu.lan', port: '2222' };
  _envState.servers = [host, vm];
  _envState.remoteHost = 'gpu.lan';
  _envState.remoteServerKey = _serverKey(host);
  headerPicker([host, vm], _serverKey(vm));
  const request = await download(undefined);
  assert.equal(request.remote_host, 'gpu.lan');
  assert.equal(request.ssh_port, '2222');
  assert.equal(request.remote_server_key, _serverKey(vm));
});

test('re-downloading checks the old session on the profile that ran it', async () => {
  const host = { name: 'gpu', host: 'gpu.lan', port: '22' };
  const vm = { name: 'gpu-vm', host: 'gpu.lan', port: '2222' };
  _envState.servers = [host, vm];
  localStorage.setItem('cookbook-tasks', JSON.stringify([{
    id: 'dl-old', sessionId: 'dl-old', name: 'model-7b', type: 'download', status: 'done', ts: 1,
    remoteHost: 'gpu.lan', remoteServerKey: _serverKey(vm), payload: { repo_id: 'org/model-7b', remote_host: 'gpu.lan' },
  }]));
  const before = probes.length;
  await download('gpu.lan');
  await waitFor(() => probes.length > before, { what: 'the old-session probe' });
  assert.match(probes.at(-1), /^ssh -p 2222 gpu\.lan '.*tmux has-session -t dl-old/);
});
