// The Cookbook reads a failed serve's output and offers fixes
// (static/js/cookbook-diagnosis.js _diagnose). A fix that runs a command
// launches it on the server through /api/model/serve.
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

const launched = [];
fake.route('POST', '/api/model/serve', ({ body }) => {
  launched.push(JSON.parse(body).cmd);
  return { ok: true, session_id: `fix-${launched.length}` };
});
fake.route('GET', /^\/api\//, () => ({}));
fake.route('POST', /^\/api\//, () => ({}));

document.body.innerHTML = '<div id="toast"></div>';
await import('../../../../static/js/cookbook.js');
const { _diagnose } = await import('../../../../static/js/cookbook-diagnosis.js');

// Split a command the way a POSIX shell does. Unquoted redirection and
// control characters become their own tokens, so a version spec that the
// shell would read as a redirect shows up as one.
function shellTokens(line) {
  const out = [];
  let word = null;
  const flush = () => { if (word !== null) out.push(word); word = null; };
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === "'") {
      const end = line.indexOf("'", i + 1);
      word = (word ?? '') + line.slice(i + 1, end);
      i = end;
    } else if (c === '"') {
      const end = line.indexOf('"', i + 1);
      word = (word ?? '') + line.slice(i + 1, end);
      i = end;
    } else if (/\s/.test(c)) {
      flush();
    } else if ('<>|;&'.includes(c)) {
      flush();
      out.push({ op: c });
    } else {
      word = (word ?? '') + c;
    }
  }
  flush();
  return out;
}

async function runFix(output, label) {
  const diagnosis = _diagnose(output);
  assert.ok(diagnosis, 'the output is diagnosed');
  const fix = diagnosis.fixes.find((f) => f.label === label);
  assert.ok(fix, `the diagnosis offers "${label}": ${diagnosis.fixes.map((f) => f.label)}`);
  const before = launched.length;
  await fix.action(document.createElement('div'));
  await waitFor(() => launched.length === before + 1, { what: 'the fix launch' });
  return launched.at(-1);
}

test('the kernels repair installs kernels<0.15 instead of redirecting from a file named 0.15', async () => {
  const cmd = await runFix(
    'ValueError: Either a revision or a version must be specified\n  File "transformers/integrations/hub_kernels.py"',
    'Repair kernel package',
  );
  const tokens = shellTokens(cmd);
  assert.deepEqual(tokens.filter((t) => typeof t !== 'string'), [], `no shell operators in: ${cmd}`);
  assert.equal(tokens.at(-1), 'kernels<0.15');
  assert.deepEqual(tokens.slice(0, 4), ['python3', '-m', 'pip', 'install']);
});

test('a missing SGLang native dependency offers the sglang-kernel repair', async () => {
  const output = [
    '/tmp/cuda_utils.c:7:10: fatal error: Python.h: No such file or directory',
    'ImportError:',
    '[sgl_kernel] CRITICAL: Could not load any common_ops library!',
    'Please ensure sgl_kernel is properly installed with:',
    'pip install --upgrade sglang-kernel',
    '- ImportError: libnuma.so.1: cannot open shared object file',
  ].join('\n');
  const cmd = await runFix(output, 'Repair sglang-kernel');
  assert.deepEqual(shellTokens(cmd), [
    'python3', '-m', 'pip', 'install', '-U', '--force-reinstall', '--no-cache-dir', 'sglang-kernel',
  ]);
});
