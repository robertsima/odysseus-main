// The serve command the Cookbook builds for each engine (static/js/cookbook.js
// _buildServeCmd), and the server-profile helpers it and the other Cookbook
// tabs use to decide which machine a command targets.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
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

const cookbook = await import('../../../../static/js/cookbook.js');
const hwfit = await import('../../../../static/js/cookbook-hwfit.js');
const { _buildServeCmd, _envState, _getPlatform, _serverKey, _serverByVal, _selectedServer } = cookbook;

const FRESH_ENV = { ..._envState };
beforeEach(() => {
  for (const key of Object.keys(_envState)) delete _envState[key];
  Object.assign(_envState, structuredClone(FRESH_ENV));
});

// Run the hardware scan the Cookbook's What-Fits tab runs, answering that the
// current target has an NVIDIA (CUDA) GPU. CUDA-only flags are emitted only
// after such a scan of the same host.
async function scanCudaHardware() {
  document.body.innerHTML = '<div id="hwfit-list"></div><div id="hwfit-hw"></div>';
  fake.route('GET', '/api/model/cached', () => ({ models: [] }));
  fake.route('GET', '/api/cookbook/ollama/library', () => ({ models: [] }));
  fake.route('GET', '/api/hwfit/models', () => ({ system: { backend: 'cuda' }, models: [] }));
  await hwfit._hwfitFetch(true);
}

function words(cmd) {
  return cmd.split(/\s+/);
}

// Undo _shellQuote: '...' with '\'' for each embedded quote.
function unquoteShell(arg) {
  assert.ok(arg.startsWith("'") && arg.endsWith("'"), `expected a single-quoted argument: ${arg.slice(0, 40)}`);
  return arg.slice(1, -1).replace(/'\\''/g, "'");
}

test('a CPU-only llama.cpp serve carries no flash-attn or CUDA unified-memory setting', async () => {
  await scanCudaHardware();
  const gpu = _buildServeCmd({ ngl: '99', flash_attn: true, unified_mem: true }, 'org/m-GGUF', 'llamacpp');
  assert.match(gpu, /GGML_CUDA_ENABLE_UNIFIED_MEMORY=1/, 'the GPU serve keeps the unified-memory env');
  assert.match(gpu, /--flash-attn on/);

  const cpu = _buildServeCmd({ ngl: '0', flash_attn: true, unified_mem: true }, 'org/m-GGUF', 'llamacpp');
  assert.match(cpu, /-ngl 0\b/);
  assert.doesNotMatch(cpu, /GGML_CUDA_ENABLE_UNIFIED_MEMORY/);
  assert.doesNotMatch(cpu, /--flash-attn/);

  const cpuMode = _buildServeCmd({ llama_mode: 'cpu', ngl: '99', flash_attn: true }, 'org/m-GGUF', 'llamacpp');
  assert.match(cpuMode, /-ngl 0\b/, 'the CPU inference mode forces zero GPU layers');
  assert.doesNotMatch(cpuMode, /--flash-attn/);
});

test('vLLM gets --swap-space only for a positive swap size', () => {
  for (const swap of ['', '0', 'off', 'none', 'false', undefined]) {
    const cmd = _buildServeCmd({ swap }, 'org/model', 'vllm');
    assert.doesNotMatch(cmd, /--swap-space/, `swap=${JSON.stringify(swap)}`);
  }
  assert.match(_buildServeCmd({ swap: '4' }, 'org/model', 'vllm'), /--swap-space 4\b/);
});

test('Diffusers on a Windows host runs python, which exists there, not python3', () => {
  _envState.hostPlatform = 'windows';
  const win = _buildServeCmd({}, 'org/sdxl', 'diffusers');
  assert.equal(words(win)[0], 'python');
  assert.match(win, /^python scripts\/diffusion_server\.py --model org\/sdxl /);

  _envState.hostPlatform = '';
  assert.equal(words(_buildServeCmd({}, 'org/sdxl', 'diffusers'))[0], 'python3');
});

test('llama.cpp on the local Windows host runs native llama-server; a remote Windows host gets llama_cpp.server', () => {
  // The local host reports Windows; a hardware scan of a Linux box left a
  // stale env platform behind.
  _envState.hostPlatform = 'windows';
  _envState.platform = 'linux';
  _envState.servers = [{ host: 'winbox', platform: 'windows' }];

  const local = _buildServeCmd({ host: '' }, 'org/m-GGUF', 'llamacpp');
  assert.match(local, /^llama-server --model /);

  const remote = _buildServeCmd({ host: 'winbox' }, 'org/m-GGUF', 'llamacpp');
  assert.match(remote, /^python -m llama_cpp\.server --model /);
});

test('the local platform comes from the backend host, not from the last scanned server', () => {
  _envState.hostPlatform = '';
  _envState.platform = 'windows';
  assert.equal(_getPlatform('local'), '');
  assert.equal(_getPlatform({ remoteHost: '', platform: 'windows' }), '', 'a local task is the backend host');

  _envState.hostPlatform = 'windows';
  _envState.platform = 'linux';
  assert.equal(_getPlatform('local'), 'windows');
  assert.equal(_getPlatform(), 'windows', 'with no remote host selected, the target is local');
});

test('Gemma 4 on vLLM and SGLang gets the thinking chat template', () => {
  for (const backend of ['vllm', 'sglang']) {
    const cmd = _buildServeCmd({}, 'google/gemma-4-27b-it', backend);
    const match = cmd.match(/--chat-template ('(?:[^']|'\\'')*')/);
    assert.ok(match, `${backend} passes --chat-template`);
    const template = unquoteShell(match[1]);
    // Thinking is switched on by <|think|> in the system turn, and the
    // generation prompt opens the model turn on the thought channel.
    assert.match(template, /<\|turn>system\n<\|think\|>\{\{ message\['content'\] \}\}<turn\|>/);
    assert.match(template, /\{% if add_generation_prompt %\}<\|turn>model\n<\|channel>thought\{% endif %\}$/);
    assert.doesNotMatch(template, /<\|turn>model\n<\|think\|>/);
  }
  for (const backend of ['vllm', 'sglang']) {
    assert.doesNotMatch(_buildServeCmd({}, 'Qwen/Qwen3-8B', backend), /--chat-template/);
  }
});

test('two server profiles on the same host stay distinct', () => {
  // Same host and name, reached on two SSH ports (a host and a VM behind it).
  const a = { name: 'gpu', host: 'gpu.lan', port: '22' };
  const b = { name: 'gpu', host: 'gpu.lan', port: '2222' };
  _envState.servers = [a, b];

  assert.notEqual(_serverKey(a), _serverKey(b));
  assert.equal(_serverByVal(_serverKey(a)), a);
  assert.equal(_serverByVal(_serverKey(b)), b);
});

test('host lookups follow the selected profile, not the first server with that host', () => {
  const a = { name: 'gpu', host: 'gpu.lan', port: '22', platform: 'linux' };
  const b = { name: 'gpu', host: 'gpu.lan', port: '2222', platform: 'windows' };
  _envState.servers = [a, b];
  _envState.remoteHost = 'gpu.lan';
  _envState.remoteServerKey = _serverKey(b);
  assert.equal(_selectedServer(), b);
  assert.equal(_getPlatform('gpu.lan'), 'windows');
});
