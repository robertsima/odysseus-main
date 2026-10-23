// The sidebar navigation is three groups (Work, Agents, More). An order saved
// when it was one flat list must still load, and reordering never moves an
// item out of its group.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());
const nav = await import('../../../../static/js/navOrder.js');

const GROUPS = {
  'work-section': ['tool-today-btn', 'tool-library-btn', 'tool-notes-btn', 'tool-tasks-btn', 'tool-calendar-btn'],
  'agents-section': ['tool-agents-btn', 'tool-workbench-btn'],
  'more-section': ['tool-memory-btn', 'tool-lotus-btn', 'tool-compare-btn', 'tool-cookbook-btn', 'tool-research-btn', 'tool-gallery-btn'],
};

function fakeDocument() {
  const nodes = new Map();
  const doc = {
    getElementById: (id) => nodes.get(id) || null,
    createComment: () => ({ comment: true, remove() { this.parentNode.remove(this); } }),
  };
  const container = (id, ids) => {
    const root = {
      id, ownerDocument: doc, all: [],
      get children() { return this.all.filter((n) => !n.comment); },
      insertBefore(n, before) { this.all.splice(this.all.indexOf(before), 0, n); n.parentNode = this; },
      remove(n) { this.all.splice(this.all.indexOf(n), 1); n.parentNode = null; },
    };
    nodes.set(id, root);
    root.all = ids.map((nodeId) => {
      const n = { id: nodeId, parentNode: root, remove() { this.parentNode.remove(this); } };
      nodes.set(nodeId, n);
      return n;
    });
    return root;
  };
  const roots = Object.fromEntries(Object.entries(GROUPS).map(([id, ids]) => [id, container(id, ids)]));
  return { doc, ids: (groupId) => roots[groupId].children.map((n) => n.id) };
}

beforeEach(() => localStorage.clear());

test('an order saved from the old flat list lands each item in its own group', () => {
  // A person had put Tasks first, Gallery before Brain and Workbench above Agents.
  const flat = ['search', 'new-chat', 'command', 'tasks', 'workbench', 'agents', 'gallery', 'memory', 'today',
    'calendar', 'compare', 'cookbook', 'research', 'email', 'library', 'lotus', 'notes', 'theme'];
  localStorage.setItem('odysseus-nav-order-v1', JSON.stringify({ value: flat, updated_at: 5 }));
  const { doc, ids } = fakeDocument();
  nav.applyNavOrder(nav.readNavOrder(), doc);
  assert.deepEqual(ids('work-section'), ['tool-tasks-btn', 'tool-today-btn', 'tool-calendar-btn', 'tool-library-btn', 'tool-notes-btn']);
  assert.deepEqual(ids('agents-section'), ['tool-workbench-btn', 'tool-agents-btn']);
  assert.deepEqual(ids('more-section'), ['tool-gallery-btn', 'tool-memory-btn', 'tool-compare-btn', 'tool-cookbook-btn', 'tool-research-btn', 'tool-lotus-btn']);
});

test('the old default order is not kept as a preference', () => {
  const legacyDefault = ['search', 'new-chat', 'command', 'today', 'calendar', 'compare', 'cookbook', 'research',
    'email', 'gallery', 'library', 'memory', 'lotus', 'agents', 'workbench', 'notes', 'tasks', 'theme'];
  localStorage.setItem('odysseus-nav-order-v1', JSON.stringify({ value: legacyDefault, updated_at: 5 }));
  assert.deepEqual(nav.readNavOrder(), nav.defaultNavOrder());
  const { doc, ids } = fakeDocument();
  nav.applyNavOrder(nav.readNavOrder(), doc);
  assert.deepEqual(ids('work-section'), GROUPS['work-section']);
});

test('reordering inside a group never moves an item into another group', () => {
  // Put every More item ahead of every Work item.
  const order = [...nav.defaultNavOrder()].sort((a, b) => {
    const rank = (key) => (nav.NAV_ITEMS.find((i) => i.key === key)?.group === 'more' ? 0 : 1);
    return rank(a) - rank(b);
  });
  nav.writeNavOrder(order, { sync: false });
  const { doc, ids } = fakeDocument();
  nav.applyNavOrder(nav.readNavOrder(), doc);
  for (const [groupId, members] of Object.entries(GROUPS)) {
    assert.deepEqual([...ids(groupId)].sort(), [...members].sort(), `${groupId} lost or gained an item`);
  }
});

test('every sidebar item belongs to exactly one group and Theme has none', () => {
  const sidebar = nav.NAV_ITEMS.filter((item) => item.sidebar);
  assert.ok(sidebar.every((item) => nav.NAV_GROUPS[item.group]));
  assert.equal(nav.NAV_ITEMS.find((item) => item.key === 'theme').sidebar, null);
  assert.deepEqual(sidebar.map((item) => item.sidebar).sort(), Object.values(GROUPS).flat().sort());
});
