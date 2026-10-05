// The Serve panel a cached model opens in the Cookbook
// (static/js/cookbookServe.js). It offers the engines the target can run,
// previews the launch command for the server picked in the Cookbook header,
// and launches on that server.
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

// spinner.js draws on a canvas; happy-dom has no 2D context.
const noop2d = new Proxy({}, { get: () => () => ({ addColorStop() {} }) });
HTMLCanvasElement.prototype.getContext = () => noop2d;

let cachedModels = [];
const served = [];
fake.route('GET', '/api/model/cached', () => ({ models: cachedModels }));
// One visible GPU, so the launch's no-GPU confirmation stays closed.
fake.route('GET', '/api/cookbook/gpus', () => ({ gpus: [{ index: 0 }] }));
// The port-in-use probe finds nothing listening.
fake.route('POST', '/api/shell/exec', () => ({ exit_code: 0, stdout: '' }));
fake.route('POST', '/api/model/serve', ({ body }) => {
  served.push(JSON.parse(body));
  return { ok: true, session_id: `serve-${served.length}` };
});
fake.route('GET', /^\/api\//, () => ({}));

const cookbook = await import('../../../../static/js/cookbook.js');
const serve = await import('../../../../static/js/cookbookServe.js');
const { _envState, _serverKey } = cookbook;

const FRESH_ENV = structuredClone({ ..._envState });
afterEach(() => {
  for (const key of Object.keys(_envState)) delete _envState[key];
  Object.assign(_envState, structuredClone(FRESH_ENV));
  localStorage.clear();
});

// The Cookbook header holds the server picker (#hwfit-server-select); the
// Serve tab lists cached models in #hwfit-cached-list.
async function openServePanel(model, { servers = [], selected = 'local' } = {}) {
  _envState.servers = servers;
  const options = ['<option value="local">Local</option>']
    .concat(servers.map((s) => `<option value="${_serverKey(s)}">${s.name || s.host}</option>`));
  document.body.innerHTML = `
    <div id="toast"></div>
    <div id="cookbook-modal">
      <select id="hwfit-server-select">${options.join('')}</select>
      <div id="serve-tags"></div>
      <div id="hwfit-cached-list"></div>
    </div>`;
  document.getElementById('hwfit-server-select').value = selected;
  cachedModels = [{ status: 'ready', size: '4 GB', path: '', ...model }];
  await serve._fetchCachedModels(true);
  const card = document.querySelector(`.memory-item[data-repo="${model.repo_id}"]`);
  card.click();
  await waitFor(() => card.querySelector('.hwfit-serve-panel')?._cmd, { what: 'the serve panel' });
  return card.querySelector('.hwfit-serve-panel');
}

function field(panel, name) {
  return panel.querySelector(`[data-field="${name}"]`);
}

function setField(panel, name, value) {
  const el = field(panel, name);
  if (el.type === 'checkbox') el.checked = value;
  else el.value = value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

function engines(panel) {
  return [...field(panel, 'backend').options].map((o) => o.value);
}

function previewedCommand(panel) {
  // The preview splits the command over lines with trailing backslashes.
  return panel.querySelector('.hwfit-serve-cmd').value.split(/\s*\\\r?\n/).join(' ');
}

const GGUF = { repo_id: 'org/Vis-GGUF', gguf_files: [{ rel_path: 'm-Q4_K_M.gguf', role: 'model' }] };
const GGUF_WITH_PROJECTOR = {
  repo_id: 'org/Vis-GGUF',
  gguf_files: [{ rel_path: 'm-Q4_K_M.gguf', role: 'model' }, { rel_path: 'mmproj-F16.gguf', role: 'projector' }],
};
const PLAIN = { repo_id: 'org/Plain-7B' };

test('the local Windows host offers llama.cpp and Diffusers, even after a Linux server was scanned', async () => {
  _envState.hostPlatform = 'windows';
  _envState.platform = 'linux';
  const panel = await openServePanel(PLAIN);
  assert.deepEqual(engines(panel), ['llamacpp', 'diffusers']);
});

test('a remote Windows server offers llama.cpp only, since Diffusers cannot run there', async () => {
  const winbox = { name: 'winbox', host: 'winbox', platform: 'windows' };
  const panel = await openServePanel(PLAIN, { servers: [winbox], selected: _serverKey(winbox) });
  assert.deepEqual(engines(panel), ['llamacpp']);
});

test('a Linux server offers the GPU engines and Diffusers', async () => {
  const gpu = { name: 'gpu', host: 'gpu.lan', platform: 'linux' };
  const panel = await openServePanel(PLAIN, { servers: [gpu], selected: _serverKey(gpu) });
  for (const engine of ['vllm', 'sglang', 'llamacpp', 'diffusers']) assert.ok(engines(panel).includes(engine), engine);
});

test('the preview targets the server picked in the header, not a stale remote host', async () => {
  _envState.remoteHost = '';
  const winbox = { name: 'winbox', host: 'winbox', platform: 'windows' };
  const panel = await openServePanel(GGUF, { servers: [winbox], selected: _serverKey(winbox) });
  setField(panel, 'backend', 'llamacpp');
  assert.equal(field(panel, 'host').value, 'winbox');
  // A remote Windows host runs the Python server; the local host would get llama-server.
  assert.match(previewedCommand(panel), /^python -m llama_cpp\.server --model /);
  assert.equal(panel._cmd, previewedCommand(panel), 'the launch command is the previewed one');
});

test('vision serves the projector found by the cached-model scan', async () => {
  const panel = await openServePanel(GGUF_WITH_PROJECTOR);
  setField(panel, 'backend', 'llamacpp');
  setField(panel, 'vision', true);
  const cmd = previewedCommand(panel);
  assert.ok(
    cmd.includes(`--mmproj "$(printf %s \${HOME}'/.cache/huggingface/hub/models--org--Vis-GGUF/snapshots/mmproj-F16.gguf')"`),
    cmd,
  );
  assert.doesNotMatch(cmd, /\bfind\b/, 'no runtime search, which the serve-command validator rejects');
  assert.equal(panel.querySelector('.hwfit-serve-vision-warn').style.display, 'none');
});

test('vision without a scanned projector warns and does not launch', async () => {
  const panel = await openServePanel(GGUF);
  setField(panel, 'backend', 'llamacpp');
  setField(panel, 'vision', true);
  assert.doesNotMatch(previewedCommand(panel), /--mmproj|--clip_model_path/);
  assert.equal(panel.querySelector('.hwfit-serve-vision-warn').style.display, 'flex');

  const before = fake.calls.length;
  panel.querySelector('.hwfit-serve-launch').click();
  await waitFor(() => document.getElementById('toast').classList.contains('show'), { what: 'the toast' });
  assert.match(document.getElementById('toast').textContent, /mmproj/);
  assert.deepEqual(fake.calls.slice(before).filter((c) => c.url.pathname === '/api/model/serve'), []);
});

test('Launch probes and serves on the server picked in the header, with its SSH port', async () => {
  // Two profiles for one host. The header shows the second; the remembered
  // remote server is the first.
  const host = { name: 'gpu', host: 'gpu.lan', port: '22', platform: '' };
  const vm = { name: 'gpu-vm', host: 'gpu.lan', port: '2222', platform: 'linux' };
  _envState.remoteHost = 'gpu.lan';
  _envState.remoteServerKey = _serverKey(host);
  const panel = await openServePanel(PLAIN, { servers: [host, vm], selected: _serverKey(vm) });
  setField(panel, 'backend', 'vllm');
  const callsBefore = fake.calls.length;
  const servedBefore = served.length;
  panel.querySelector('.hwfit-serve-launch').click();
  await waitFor(() => served.length === servedBefore + 1, { what: 'the serve request' });

  const calls = fake.calls.slice(callsBefore);
  const gpuProbe = calls.find((c) => c.url.pathname === '/api/cookbook/gpus');
  assert.equal(gpuProbe.url.searchParams.get('host'), 'gpu.lan');
  assert.equal(gpuProbe.url.searchParams.get('ssh_port'), '2222');
  const portProbe = calls.find((c) => c.url.pathname === '/api/shell/exec');
  assert.match(JSON.parse(portProbe.body).command, /^ssh .*-p 2222 gpu\.lan /);
  const request = served.at(-1);
  assert.equal(request.remote_host, 'gpu.lan');
  assert.equal(request.ssh_port, '2222');
  assert.equal(request.platform, 'linux');
  assert.match(request.cmd, /^vllm serve org\/Plain-7B /);
});
