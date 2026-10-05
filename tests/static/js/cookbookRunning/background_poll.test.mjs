// The Cookbook's background status poll (static/js/cookbookRunning.js,
// _startBackgroundMonitor). It folds GET /api/cookbook/tasks/status into the
// task list the Running tab shows, and registers a model endpoint for each
// serve that turned ready. It runs with the Cookbook closed, so nothing else
// corrects a task it marks wrongly.
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

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

function task(sessionId, type, extra = {}) {
  return { id: sessionId, sessionId, name: sessionId, type, status: 'running', output: '', ts: 1, remoteHost: '', ...extra };
}

const LOCAL_TASKS = [
  // Dependency installs (pip) report success by the runner's exit sentinel.
  task('dep-ok', 'download', { payload: { _dep: true, repo_id: 'vllm' }, output: 'Collecting vllm\n' }),
  task('dep-failed', 'download', { payload: { _dep: true, repo_id: 'vllm' } }),
  task('dep-no-sentinel', 'download', { payload: { _dep: true, repo_id: 'rembg' } }),
  // Model downloads report success by DOWNLOAD_OK.
  task('dl-ok', 'download', { payload: { repo_id: 'org/a' } }),
  task('dl-cut-off', 'download', { payload: { repo_id: 'org/b' } }),
  task('dl-completed', 'download', { payload: { repo_id: 'org/c' } }),
  task('dl-error', 'download', { payload: { repo_id: 'org/d' } }),
  // Serves that turned ready.
  task('serve-local', 'serve', { payload: { repo_id: 'org/m', _cmd: 'vllm serve org/m --host 0.0.0.0 --port 8011' } }),
  task('serve-remote', 'serve', { remoteHost: 'me@gpu1', payload: { repo_id: 'org/m', _cmd: 'vllm serve org/m --host 0.0.0.0 --port 8012' } }),
  task('serve-ollama', 'serve', { remoteHost: 'gpu2', payload: { repo_id: 'qwen3', _cmd: 'ollama serve' } }),
];

const LIVE = [
  { session_id: 'dep-ok', type: 'download', status: 'stopped', output_tail: 'Successfully installed vllm-0.6.0\n=== Process exited with code 0 ===' },
  { session_id: 'dep-failed', type: 'download', status: 'stopped', output_tail: 'ERROR: No matching distribution found for vllm\n=== Process exited with code 1 ===' },
  { session_id: 'dep-no-sentinel', type: 'download', status: 'stopped', output_tail: 'Successfully installed rembg-2.0.0' },
  { session_id: 'dl-ok', type: 'download', status: 'stopped', output_tail: 'Fetching 4 files: 100%\nDOWNLOAD_OK' },
  { session_id: 'dl-cut-off', type: 'download', status: 'stopped', output_tail: 'model-00001-of-00004.safetensors: 100%\n/root/.cache/huggingface/hub/models--org--b/snapshots/abc' },
  { session_id: 'dl-completed', type: 'download', status: 'completed', output_tail: 'Fetching 4 files: 100%' },
  { session_id: 'dl-error', type: 'download', status: 'error', output_tail: 'HTTP 401' },
  { session_id: 'serve-local', type: 'serve', status: 'ready', remote: 'local', model: 'org/m' },
  { session_id: 'serve-remote', type: 'serve', status: 'ready', remote: 'me@gpu1', model: 'org/m' },
  { session_id: 'serve-ollama', type: 'serve', status: 'ready', remote: 'gpu2', model: 'qwen3',
    output: 'Ollama API ready on port 11500: http://0.0.0.0:11500' },
];

const registered = [];
let finalTasks;

before(async () => {
  document.body.innerHTML = '<div id="toast"></div>';
  localStorage.setItem('cookbook-tasks', JSON.stringify(LOCAL_TASKS));
  fake.route('GET', '/api/cookbook/state', () => ({ tasks: [] }));
  fake.route('POST', '/api/cookbook/state', () => ({ ok: true }));
  fake.route('GET', '/api/cookbook/tasks/status', () => ({ tasks: LIVE }));
  fake.route('GET', '/api/model-endpoints', () => []);
  fake.route('POST', '/api/model-endpoints', ({ body }) => {
    registered.push(Object.fromEntries(body.entries()));
    return { id: `ep-${registered.length}` };
  });
  fake.route('GET', /^\/api\//, () => ({}));

  await import('../../../../static/js/cookbook.js');
  const running = await import('../../../../static/js/cookbookRunning.js');
  running._startBackgroundMonitor();
  await waitFor(() => registered.length === 3, { what: "three endpoint registrations" });
  finalTasks = new Map(running._loadTasks().map((t) => [t.sessionId, t]));
});

function statusOf(sessionId) {
  return finalTasks.get(sessionId).status;
}

test('a dependency install that exited 0 is done even though its session is gone', () => {
  assert.equal(statusOf('dep-ok'), 'done');
});

test('a dependency install that exited 1 is not shown as done', () => {
  assert.notEqual(statusOf('dep-failed'), 'done');
  assert.equal(statusOf('dep-failed'), 'crashed');
});

test("a dependency install with pip's success line and no exit sentinel is done", () => {
  assert.equal(statusOf('dep-no-sentinel'), 'done');
});

test('a download that printed DOWNLOAD_OK is done even though its session is gone', () => {
  assert.equal(statusOf('dl-ok'), 'done');
});

test('a download cut off after a snapshot path, without DOWNLOAD_OK, is crashed', () => {
  assert.equal(statusOf('dl-cut-off'), 'crashed');
});

test('completed and error statuses from the server reach the local task list', () => {
  assert.equal(statusOf('dl-completed'), 'done');
  assert.equal(statusOf('dl-error'), 'error');
});

function registration(name) {
  const byUrl = new Map(registered.map((r) => [r.base_url, r]));
  return byUrl.get(name);
}

test('a ready local serve registers http://localhost and is marked container-local', () => {
  const ep = registration('http://localhost:8011/v1');
  assert.ok(ep, `registered: ${registered.map((r) => r.base_url)}`);
  assert.equal(ep.container_local, 'true');
});

test('a ready remote serve registers the bare host, not user@host, and is not container-local', () => {
  const ep = registration('http://gpu1:8012/v1');
  assert.ok(ep, `registered: ${registered.map((r) => r.base_url)}`);
  assert.equal(ep.container_local, undefined);
});

test('an Ollama serve advertising 0.0.0.0 registers the host it runs on', () => {
  const ep = registration('http://gpu2:11500/v1');
  assert.ok(ep, `registered: ${registered.map((r) => r.base_url)}`);
  assert.equal(ep.container_local, undefined);
});
