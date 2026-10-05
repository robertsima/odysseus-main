// providerLabel() names a remote provider by host and never names the serving
// tool from a port. vLLM, SGLang, llama.cpp and plain OpenAI-compatible servers
// all share 8000 and 8080, so a port-only label would show a vLLM box on :8080
// as "llama.cpp". Loopback and private-LAN hosts read "Local".
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { providerLabel } from '../../../../static/js/providers.js';

const CASES = [
  ['http://localhost:8080/v1', 'Local'],
  ['http://127.0.0.1:8080/v1', 'Local'],
  ['http://localhost:8000/v1', 'Local'],
  ['http://localhost:1234/v1', 'Local'],
  ['http://localhost:11434/api', 'Local'],
  ['http://localhost:9999/v1', 'Local'],
  ['http://192.168.1.50:8080', 'Local'],
  ['https://api.openai.com/v1', 'OpenAI'],
  ['https://api.groq.com/openai/v1', 'Groq'],
];

for (const [url, expected] of CASES) {
  test(`${url} is labelled ${expected}`, () => {
    assert.equal(providerLabel(url), expected);
  });
}
