// The navigation layout model: migration from the flat order older builds
// saved, the edit operations, tolerance for ids this build does not know, and
// agreement with the account (adopt a newer layout, push a newer local one).
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
after(() => dom.restore());
const L = await import('../../../../static/js/navLayout.js');

const ids = (layout, groupId) => layout.groups.find((group) => group.id === groupId).items;
const store = (key, value, at = 5) => localStorage.setItem(key, JSON.stringify({ value, updated_at: at }));

let fake;
let account;
beforeEach(() => {
  localStorage.clear();
  L._resetForTests();
  account = new Map();
  fake?.restore();
  fake = installFetchFake();
  const keyOf = (url) => url.pathname.split('/').pop();
  fake.route('GET', /^\/api\/prefs\/[\w-]+$/, ({ url }) => ({ key: keyOf(url), value: account.get(keyOf(url)) ?? null }));
  fake.route('PUT', /^\/api\/prefs\/[\w-]+$/, ({ url, body }) => {
    account.set(keyOf(url), JSON.parse(body).value);
    return jsonResponse({ ok: true });
  });
});

// ── Migration ────────────────────────────────────────────────────────────────

test('a v1 order saved as the old default migrates to the default layout', () => {
  const legacyDefault = ['search', 'new-chat', 'command', 'today', 'calendar', 'compare', 'cookbook', 'research',
    'email', 'gallery', 'library', 'memory', 'lotus', 'agents', 'workbench', 'notes', 'tasks', 'theme'];
  store(L.LEGACY_ORDER_KEY, legacyDefault);
  assert.deepEqual(L.readLayout(), L.defaultLayout());
});

test('a customized v1 order keeps built-in membership and the saved order inside each group', () => {
  // Tasks first, Gallery before Brain, Workbench above Agents.
  const flat = ['search', 'new-chat', 'command', 'tasks', 'workbench', 'agents', 'gallery', 'memory', 'today',
    'calendar', 'compare', 'cookbook', 'research', 'email', 'library', 'lotus', 'notes', 'theme'];
  store(L.LEGACY_ORDER_KEY, flat);
  const layout = L.readLayout();
  assert.deepEqual(layout.groups.map((group) => group.id), ['work', 'agents', 'more']);
  assert.deepEqual(ids(layout, 'work'), ['tasks', 'today', 'calendar', 'email', 'library', 'notes']);
  assert.deepEqual(ids(layout, 'agents'), ['workbench', 'agents']);
  assert.deepEqual(ids(layout, 'more'), ['gallery', 'memory', 'compare', 'cookbook', 'research', 'lotus']);
  // The retired fixed controls are not tools.
  assert.ok(!layout.groups.flatMap((group) => group.items).some((key) => ['search', 'new-chat', 'command', 'theme'].includes(key)));
});

test('a short v1 order is a choice about those tools only; the rest stay where the default puts them', () => {
  store(L.LEGACY_ORDER_KEY, ['tasks', 'agents']);
  const layout = L.readLayout();
  assert.equal(ids(layout, 'work')[0], 'tasks');
  assert.deepEqual(ids(layout, 'more'), ['memory', 'lotus', 'compare', 'cookbook', 'research', 'gallery']);
});

test('the fold state the sidebar kept separately carries into the layout', () => {
  store(L.LEGACY_ORDER_KEY, ['tasks', 'today']);
  localStorage.setItem('section-collapsed', JSON.stringify({ 'work-section': true, 'more-section': false }));
  const layout = L.readLayout();
  assert.equal(layout.groups.find((g) => g.id === 'work').collapsed, true);
  assert.equal(layout.groups.find((g) => g.id === 'more').collapsed, false);
});

test('reading the layout never removes the v1 order it came from', () => {
  store(L.LEGACY_ORDER_KEY, ['tasks', 'today']);
  L.readLayout();
  assert.ok(localStorage.getItem(L.LEGACY_ORDER_KEY), 'the v1 key must survive');
});

test('an account that only has the v1 order is migrated and the result is saved to the account', async () => {
  account.set('nav-order', { value: ['tasks', 'workbench', 'agents', 'today'], updated_at: 700 });
  const layout = await L.syncLayoutWithAccount();
  assert.equal(ids(layout, 'work')[0], 'tasks');
  assert.deepEqual(ids(layout, 'agents'), ['workbench', 'agents']);
  await waitFor(() => account.has('nav-layout'), { what: 'the migrated layout to reach the account' });
  assert.equal(account.get('nav-layout').value.version, 2);
  assert.deepEqual(account.get('nav-order').value, ['tasks', 'workbench', 'agents', 'today'], 'the v1 pref is left alone');
});

// ── Edit operations ──────────────────────────────────────────────────────────

test('create adds an empty group at the end with the first-letter icon', () => {
  const { layout, id } = L.createGroup(L.defaultLayout(), { label: '  Clients  ' });
  const group = layout.groups.at(-1);
  assert.equal(group.id, id);
  assert.deepEqual({ label: group.label, icon: group.icon, items: group.items, collapsed: group.collapsed }, { label: 'Clients', icon: 'monogram', items: [], collapsed: false });
  assert.ok(!['work', 'agents', 'more'].includes(id));
});

test('rename changes the label only; an empty name is refused', () => {
  const base = L.defaultLayout();
  const renamed = L.renameGroup(base, 'work', 'Daily');
  assert.equal(renamed.groups[0].label, 'Daily');
  assert.deepEqual(renamed.groups[0].items, base.groups[0].items);
  assert.equal(L.renameGroup(base, 'work', '   '), base);
});

test('groups reorder one place at a time and at the ends do nothing', () => {
  const base = L.defaultLayout();
  assert.deepEqual(L.moveGroupBy(base, 'more', -1).groups.map((g) => g.id), ['work', 'more', 'agents']);
  assert.equal(L.moveGroupBy(base, 'work', -1), base);
  assert.deepEqual(L.moveGroupRelative(base, 'more', 'work').groups.map((g) => g.id), ['more', 'work', 'agents']);
});

test('items reorder inside a group, skipping tools this account cannot see', () => {
  const base = L.defaultLayout();
  const visible = (key) => key !== 'email';
  // Work is today, email, library, ...; with Email hidden, Library moves past it in one step.
  const up = L.moveItemBy(base, 'library', -1, visible);
  assert.deepEqual(ids(up, 'work').slice(0, 3), ['library', 'today', 'email']);
  assert.equal(L.moveItemBy(base, 'today', -1), base);
});

test('an item moves to another group, before a given item or last', () => {
  const base = L.defaultLayout();
  const last = L.moveItem(base, 'tasks', 'agents');
  assert.deepEqual(ids(last, 'agents'), ['agents', 'workbench', 'tasks']);
  assert.ok(!ids(last, 'work').includes('tasks'));
  const before = L.moveItem(base, 'tasks', 'agents', 'workbench');
  assert.deepEqual(ids(before, 'agents'), ['agents', 'tasks', 'workbench']);
});

test('an item can move into an empty group', () => {
  const { layout, id } = L.createGroup(L.defaultLayout(), { label: 'Clients' });
  const moved = L.moveItem(layout, 'notes', id);
  assert.deepEqual(ids(moved, id), ['notes']);
  assert.ok(!ids(moved, 'work').includes('notes'));
});

test('deleting a group moves its tools to the chosen group and never drops one', () => {
  const base = L.defaultLayout();
  const all = (layout) => layout.groups.flatMap((g) => g.items).sort();
  const toMore = L.deleteGroup(base, 'agents', 'more');
  assert.deepEqual(toMore.groups.map((g) => g.id), ['work', 'more']);
  assert.deepEqual(ids(toMore, 'more').slice(-2), ['agents', 'workbench']);
  assert.deepEqual(all(toMore), all(base));
  // No target given: the first remaining group takes them.
  assert.deepEqual(ids(L.deleteGroup(base, 'agents'), 'work').slice(-2), ['agents', 'workbench']);
  // The last group cannot be deleted.
  const single = L.deleteGroup(L.deleteGroup(base, 'agents'), 'more');
  assert.equal(L.deleteGroup(single, 'work'), single);
});

test('reset writes the default layout so the account stops holding the old one', async () => {
  const { layout } = L.createGroup(L.defaultLayout(), { label: 'Clients' });
  L.writeLayout(layout);
  L.resetLayout();
  assert.deepEqual(L.readLayout(), L.defaultLayout());
  await waitFor(() => account.get('nav-layout')?.value.groups.length === 3, { what: 'the reset to reach the account' });
});

// ── Safety ───────────────────────────────────────────────────────────────────

test('an id this build does not know survives edits and a round trip, but is not a known tool', () => {
  const saved = { version: 2, groups: [
    { id: 'work', label: 'Work', icon: 'work', collapsed: false, items: ['future-tool', 'today', 'notes'] },
    { id: 'more', label: 'More', icon: 'more', collapsed: true, items: ['lotus'] },
  ] };
  const normalized = L.normalizeLayout(saved);
  assert.ok(ids(normalized, 'work').includes('future-tool'));
  const edited = L.moveItem(L.moveItemBy(normalized, 'notes', -1, L.isKnownItem), 'lotus', 'work');
  assert.ok(ids(edited, 'work').includes('future-tool'), 'an edit must not drop an unknown id');
  L.writeLayout(edited, { sync: false });
  L._resetForTests();
  assert.ok(ids(L.readLayout(), 'work').includes('future-tool'));
  assert.equal(L.isKnownItem('future-tool'), false);
});

test('a tool the saved layout lacks goes to its default group at its default place', () => {
  const saved = { version: 2, groups: [
    { id: 'work', label: 'Work', icon: 'work', collapsed: false, items: ['library', 'notes', 'tasks', 'calendar'] },
    { id: 'agents', label: 'Agents', icon: 'agents', collapsed: false, items: ['agents', 'workbench'] },
    { id: 'more', label: 'More', icon: 'more', collapsed: true, items: ['memory', 'compare', 'cookbook', 'research', 'gallery'] },
  ] };
  const layout = L.normalizeLayout(saved);
  assert.deepEqual(ids(layout, 'work'), ['today', 'email', 'library', 'notes', 'tasks', 'calendar']);
  assert.deepEqual(ids(layout, 'more'), ['memory', 'lotus', 'compare', 'cookbook', 'research', 'gallery']);
});

test('a new tool whose default group was deleted goes to the first group', () => {
  const saved = { version: 2, groups: [
    { id: 'mine', label: 'Mine', icon: 'monogram', collapsed: false, items: ['today', 'email', 'library', 'notes', 'tasks', 'calendar', 'agents', 'workbench', 'memory', 'compare', 'cookbook', 'research', 'gallery'] },
  ] };
  assert.ok(ids(L.normalizeLayout(saved), 'mine').includes('lotus'));
});

test('a tool listed twice is kept once and garbage falls back to the default', () => {
  const twice = L.normalizeLayout({ version: 2, groups: [
    { id: 'a', label: 'A', items: ['notes'] }, { id: 'b', label: 'B', items: ['notes', 'tasks'] }] });
  assert.equal(twice.groups.flatMap((g) => g.items).filter((key) => key === 'notes').length, 1);
  assert.deepEqual(L.normalizeLayout('nonsense'), L.defaultLayout());
  assert.deepEqual(L.normalizeLayout({ groups: [] }), L.defaultLayout());
});

// ── Account agreement ────────────────────────────────────────────────────────

const labeled = (label) => { const layout = L.defaultLayout(); layout.groups[0].label = label; return layout; };

test('a newer account layout is adopted without being sent back', async () => {
  store(L.LAYOUT_KEY, labeled('Mine'), 100);
  account.set('nav-layout', { value: labeled('Theirs'), updated_at: 900 });
  const layout = await L.syncLayoutWithAccount();
  assert.equal(layout.groups[0].label, 'Theirs');
  assert.equal(L.getLayout().groups[0].label, 'Theirs');
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(fake.calls.filter((call) => call.method === 'PUT').length, 0);
});

test('a newer local layout is pushed to the account and kept', async () => {
  store(L.LAYOUT_KEY, labeled('Mine'), 900);
  account.set('nav-layout', { value: labeled('Theirs'), updated_at: 100 });
  const layout = await L.syncLayoutWithAccount();
  assert.equal(layout.groups[0].label, 'Mine');
  await waitFor(() => account.get('nav-layout')?.value.groups[0].label === 'Mine', { what: 'the local layout to reach the account' });
  assert.equal(account.get('nav-layout').updated_at, 900);
});

test('an account value that is not a layout is ignored', async () => {
  store(L.LAYOUT_KEY, labeled('Mine'), 100);
  account.set('nav-layout', { value: 'broken', updated_at: 900 });
  assert.equal((await L.syncLayoutWithAccount()).groups[0].label, 'Mine');
});

test('subscribers hear about a change and an unchanged edit is not written', () => {
  const heard = [];
  L.subscribe((layout) => heard.push(layout.groups.length));
  L.updateLayout((layout) => L.createGroup(layout, { label: 'X' }).layout);
  L.updateLayout((layout) => layout);
  assert.deepEqual(heard, [4]);
});
