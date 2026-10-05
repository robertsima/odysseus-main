// isLocalEndpoint() decides whether an endpoint is billed. Self-hosted endpoints
// reached by a bare Docker/Compose service name ("http://llamaswap:8000") must
// read as local, or they are priced at cloud rates against the substring-matched
// MODEL_PRICING table. Cloud hostnames stay billable.
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

const { isLocalEndpoint } = await import('../../../../static/js/chatRenderer.js');

const LOCAL = [
  'http://llamaswap:8000',
  'http://nim-nano:8000/v1',
  'http://localhost:7000',
  'http://127.0.0.1:11434',
  'http://192.168.50.244',
  'http://10.0.0.5:8080',
  'http://172.16.0.9',
  'http://server.local',
];
const BILLABLE = [
  'https://api.openai.com/v1',
  'https://openrouter.ai/api/v1',
  'https://api.anthropic.com',
  'https://generativelanguage.googleapis.com',
];

for (const url of LOCAL) {
  test(`${url} is local`, () => assert.equal(isLocalEndpoint(url), true));
}
for (const url of BILLABLE) {
  test(`${url} is billable`, () => assert.equal(isLocalEndpoint(url), false));
}
