// Pure review logic in static/js/docReview.js: section diff, placeholder scan,
// summary lines. The bugs these guard: a reordered or CRLF document reporting
// every section as changed, a code sample's "TODO" blocking an export, and a
// document with no headings showing nothing at all.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { diffSections, findPlaceholders, reviewSummary, defaultBaseline, versionLabel } from '../../../../static/js/docReview.js';

const OLD = `# Services agreement

Intro paragraph.

## 1. Scope

Design work.

## 2. Fees

| Item | Fee |
|---|---|
| Design | 1000 |

## 3. Term

Twelve months.
`;

test('a changed fee table is reported as one changed section with the line diff', () => {
  const next = OLD.replace('| Design | 1000 |', '| Design | 1200 |');
  const d = diffSections(OLD, next);
  assert.deepEqual(d.added, []);
  assert.deepEqual(d.removed, []);
  assert.equal(d.changed.length, 1);
  assert.equal(d.changed[0].heading, '2. Fees');
  const kinds = d.changed[0].diff.filter((l) => l.type !== 'same' && l.type !== 'gap').map((l) => l.type + ':' + l.text);
  assert.deepEqual(kinds, ['del:| Design | 1000 |', 'add:| Design | 1200 |']);
});

test('added and removed headings are reported by heading text', () => {
  const next = OLD.replace('## 3. Term\n\nTwelve months.\n', '## 4. Liability\n\nCapped at fees.\n');
  const d = diffSections(OLD, next);
  assert.deepEqual(d.added.map((s) => s.heading), ['4. Liability']);
  assert.deepEqual(d.removed.map((s) => s.heading), ['3. Term']);
  assert.deepEqual(d.changed, []);
});

test('reordering sections without editing them changes nothing', () => {
  const next = `# Services agreement

Intro paragraph.

## 3. Term

Twelve months.

## 1. Scope

Design work.

## 2. Fees

| Item | Fee |
|---|---|
| Design | 1000 |
`;
  const d = diffSections(OLD, next);
  assert.deepEqual([d.added, d.removed, d.changed], [[], [], []]);
});

test('CRLF text compares equal to the same text with LF', () => {
  const d = diffSections(OLD, OLD.replace(/\n/g, '\r\n'));
  assert.deepEqual([d.added, d.removed, d.changed], [[], [], []]);
});

test('a leading preamble is its own section', () => {
  const d = diffSections('Hello\n\n# A\n\nx\n', 'Hello there\n\n# A\n\nx\n');
  assert.equal(d.changed.length, 1);
  assert.equal(d.changed[0].heading, '');
});

test('a document with no headings diffs by paragraph', () => {
  const a = 'First para.\n\nSecond para.\n\nThird para.';
  const b = 'First para.\n\nSecond para, revised.\n\nThird para.';
  const d = diffSections(a, b);
  assert.equal(d.changed.length, 1);
  const moved = d.changed[0].diff.filter((l) => l.type === 'add' || l.type === 'del');
  assert.deepEqual(moved.map((l) => l.type + ':' + l.text), ['del:Second para.', 'add:Second para, revised.']);
});

test('headings inside code fences do not split sections', () => {
  const a = '# A\n\n```\n# not a heading\n```\n';
  const d = diffSections(a, a.replace('not a', 'still not a'));
  assert.equal(d.changed.length, 1);
  assert.equal(d.changed[0].heading, 'A');
  assert.deepEqual([d.added, d.removed], [[], []]);
});

test('findPlaceholders finds the four kinds with line and section', () => {
  const md = '# Deal\n\nPay [[amount]] by {{date}}.\n\n## Notes\n\nTODO confirm\nSign: ______\nthe todo_list and tbd.';
  const found = findPlaceholders(md);
  assert.deepEqual(found.map((p) => [p.text, p.line, p.section]), [
    ['[[amount]]', 3, 'Deal'],
    ['{{date}}', 3, 'Deal'],
    ['TODO', 7, 'Notes'],
    ['______', 8, 'Notes'],
    ['tbd', 9, 'Notes'],
  ]);
});

test('placeholders inside fenced code are ignored, and short underscore runs too', () => {
  const md = 'a ___ b\n\n```js\nconst x = "[[y]]"; // TODO\n```\n\n~~~\nFIXME\n~~~\nreal FIXME';
  assert.deepEqual(findPlaceholders(md).map((p) => [p.text, p.line]), [['FIXME', 10]]);
});

test('reviewSummary words each line and flags unresolved suggestions', () => {
  const d = { added: [{}], removed: [], changed: [{}, {}] };
  const s = reviewSummary({ diff: d, placeholders: [{}, {}], unresolvedSuggestions: 1 });
  assert.deepEqual(s.lines.map((l) => l.text), [
    'Changed: 3 sections', 'Placeholders: 2 remain', 'Suggestions: 1 unresolved', 'Checks: none configured',
  ]);
  assert.equal(s.needsDecision, true);
});

test('reviewSummary handles singular, none, and no baseline', () => {
  const s = reviewSummary({ diff: { added: [], removed: [], changed: [{}] }, placeholders: [{}], unresolvedSuggestions: 0 });
  assert.deepEqual(s.lines.map((l) => l.text).slice(0, 3), ['Changed: 1 section', 'Placeholders: 1 remains', 'Suggestions: none unresolved']);
  assert.equal(s.needsDecision, false);
  const none = reviewSummary({ diff: null, placeholders: [], unresolvedSuggestions: 0 });
  assert.equal(none.lines[0].text, 'Changed: nothing to compare yet');
  assert.equal(none.lines[1].text, 'Placeholders: none');
});

test('version labels and the default baseline', () => {
  const now = Date.parse('2026-10-23T12:00:00Z');
  const versions = [
    { version_number: 12, content: 'b', source: 'ai', created_at: '2026-10-23T10:00:00' },
    { version_number: 11, content: 'a', source: 'user', created_at: '2026-10-22T10:00:00' },
  ];
  assert.equal(versionLabel(versions[0], now), 'v12, 2 h ago, by agent');
  assert.equal(versionLabel(versions[1], now), 'v11, yesterday, by you');
  // Live text equals the latest saved version: compare with the one before it.
  assert.equal(defaultBaseline(versions, 'b').version_number, 11);
  // Unsaved edits: the latest saved version is the baseline.
  assert.equal(defaultBaseline(versions, 'b plus').version_number, 12);
  assert.equal(defaultBaseline(versions.slice(0, 1), 'b'), null);
});
