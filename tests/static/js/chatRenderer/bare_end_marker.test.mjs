// The Qwen bare-marker scrub in stripToolBlocks() must not eat a lone `end` (#5547).
// Its `end` branch once made both pipes optional, so Ruby, Lua and shell code
// that closes a block with a bare `end` lost those lines from the rendered
// message. Real markers (`|end`, `end|`, `|end|`, `/|end|`) still strip. The
// Python copy of the pattern has the same cases in
// tests/test_tool_parsing_bare_end_marker.py.
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

const { stripToolBlocks } = await import('../../../../static/js/chatRenderer.js');

const KEPT = [
  ['loop do\n    puts "yo"\nend\n', '\nend'],
  ['if x then\nend', '\nend'],
  ['function f()\nend\n', '\nend'],
  ['a end b', 'a end b'],
  ['append end', 'append end'],
  ['END', 'END'],
  ['\nEnd\n', 'End'],
  ['x assistant y', 'x assistant y'],
];

const STRIPPED = [
  ['a |end| b', 'a  b'],
  ['a /|end| b', 'a  b'],
  ['a |end b', 'a  b'],
  ['a end| b', 'a  b'],
  ['Before\nassistant\nAfter', 'Before \nAfter'],
  ['Before\n  assistant\t \nAfter', 'Before \nAfter'],
  ['Before\n\tassistan  \nAfter', 'Before \nAfter'],
];

for (const [text, kept] of KEPT) {
  test(`a bare end survives: ${JSON.stringify(text)}`, () => {
    assert.ok(stripToolBlocks(text).includes(kept));
  });
}
for (const [text, expected] of STRIPPED) {
  test(`a piped marker is stripped: ${JSON.stringify(text)}`, () => {
    assert.equal(stripToolBlocks(text), expected);
  });
}
