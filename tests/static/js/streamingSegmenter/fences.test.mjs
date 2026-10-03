// Fence handling in static/js/streamingSegmenter.js. While a reply streams,
// splitFinalized() decides how much of it is frozen and never re-rendered. A
// fence closes only on a marker at least as long as the one that opened it,
// so a reply that shows a ``` example inside a ```` block must stay live
// until the ```` close arrives. Freezing at the inner close cuts the outer
// code block in two.
import assert from 'node:assert/strict';
import test from 'node:test';

import { splitFinalized } from '../../../../static/js/streamingSegmenter.js';

// splitFinalized() only asks the renderer whether a prose cut changes the
// output. Rendering text as itself answers "no" for every cut, which leaves
// fence detection as the only thing deciding where the frozen part ends.
const render = (src) => src;

const INTRO = 'To show a fence inside a fence, use four backticks outside:\n\n';
const OPEN_OUTER_FENCE =
  '````markdown\n' +
  '```js\n' +
  'console.log(1);\n' +
  '```\n' +
  '\n' +
  'still inside the four-backtick example\n';

test('a shorter inner fence does not close an outer four-backtick fence', () => {
  const text = INTRO + OPEN_OUTER_FENCE;

  const finalized = splitFinalized(text, render);

  assert.equal(text.slice(0, finalized), INTRO);
});

test('the outer fence closes on a matching four-backtick line', () => {
  const text = INTRO + OPEN_OUTER_FENCE + '````\n\nAfter the example.\n';

  const finalized = splitFinalized(text, render);

  assert.equal(text.slice(finalized), 'After the example.\n');
});
