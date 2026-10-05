// The Ollama serve command the cookbook builds (static/js/cookbook.js).
// Ollama has no auth, so on this machine it must listen on loopback only;
// 0.0.0.0 is for a remote GPU host that Odysseus reaches over the network.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const { _buildServeCmd, _envState } = await import('../../../../static/js/cookbook.js');

test('a local Ollama on a custom port listens on loopback only', () => {
  _envState.remoteHost = '';

  const cmd = _buildServeCmd({ port: '11500' }, '', 'ollama');

  assert.match(cmd, /OLLAMA_HOST=127\.0\.0\.1:11500 ollama serve/);
});

test('a remote Ollama listens on every interface so Odysseus can reach it', () => {
  _envState.remoteHost = 'gpu-box';

  const cmd = _buildServeCmd({ port: '11500' }, '', 'ollama');

  assert.match(cmd, /OLLAMA_HOST=0\.0\.0\.0:11500 ollama serve/);
  _envState.remoteHost = '';
});
