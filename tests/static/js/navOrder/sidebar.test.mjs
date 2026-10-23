// The sidebar groups: the layout drawn into the page by moving the existing
// row nodes, group creation, rename, fold state, dragging (with a line where the
// tool lands, a drop zone on empty groups and no tool launch after a drop) and
// the keyboard move.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';
import { buildNavDom, groupIds, rowIds } from '../_support/navFixture.mjs';

const dom = installDom();
after(() => dom.restore());
const fake = installFetchFake();
fake.route('GET', /^\/api\/prefs\//, () => ({ value: null }));
fake.route('PUT', /^\/api\/prefs\//, () => jsonResponse({ ok: true }));
const { clicks } = buildNavDom();
const nav = await import('../../../../static/js/navOrder.js');
const L = await import('../../../../static/js/navLayout.js');
const D = await import('../../../../static/js/navDom.js');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const byId = (id) => document.getElementById(id);
const fire = (target, type, props = {}) => {
  const event = new window.Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, props);
  target.dispatchEvent(event);
  return event;
};
// happy-dom has no layout. Give rows a fixed height so "above the middle of a
// row" means something.
const layoutRows = (groupEl) => Array.from(groupEl.querySelectorAll(':scope > .list-item')).forEach((row, index) => {
  row.getBoundingClientRect = () => ({ top: index * 30, height: 30, bottom: index * 30 + 30, left: 0, right: 200, width: 200 });
});

beforeEach(async () => {
  D.setDialogs(null);
  clicks.length = 0;
  L.resetLayout();
  await sleep(0);
});

test('the default layout draws three groups holding the original row nodes', () => {
  assert.deepEqual(groupIds(document), ['work', 'agents', 'more']);
  assert.deepEqual(rowIds(document, 'work'), ['tool-today-btn', 'tool-library-btn', 'tool-notes-btn', 'tool-tasks-btn', 'tool-calendar-btn']);
  assert.deepEqual(rowIds(document, 'agents'), ['tool-agents-btn', 'tool-workbench-btn']);
  byId('tool-tasks-btn').click();
  assert.deepEqual(clicks, ['tool-tasks-btn'], 'a moved row keeps the listener the app installed');
  assert.equal(document.querySelector('#work-section .nav-group-title').textContent, 'Work');
});

test('moving a tool to another group moves its existing node, with its listener', () => {
  const node = byId('tool-notes-btn');
  L.updateLayout((layout) => L.moveItem(layout, 'notes', 'agents'));
  assert.equal(byId('tool-notes-btn'), node);
  assert.deepEqual(rowIds(document, 'agents'), ['tool-agents-btn', 'tool-workbench-btn', 'tool-notes-btn']);
  node.click();
  assert.deepEqual(clicks, ['tool-notes-btn']);
});

test('a created group is drawn after the others, empty, with a drop line and a way to open its menu', async () => {
  D.setDialogs({ styledPrompt: async () => 'Clients' });
  const id = await nav.promptNewGroup();
  const el = document.querySelector(`.nav-group[data-nav-group="${id}"]`);
  assert.ok(el, 'the new group has a container');
  assert.deepEqual(groupIds(document), ['work', 'agents', 'more', id]);
  assert.equal(el.querySelector('.nav-group-title').textContent, 'Clients');
  assert.equal(el.dataset.empty, 'true');
  assert.ok(el.querySelector('.nav-group-empty'));
  assert.ok(el.querySelector('.nav-group-menu-btn'));
});

test('the group menu renames a group and the new name is saved in the layout', async () => {
  D.setDialogs({ styledPrompt: async () => 'Daily' });
  document.querySelector('#work-section .nav-group-menu-btn').click();
  const items = Array.from(document.querySelectorAll('#nav-group-menu [role="menuitem"]'));
  assert.deepEqual(items.map((node) => node.textContent), ['Rename…', 'Move group up', 'Move group down', 'New group…', 'Edit navigation…', 'Delete group…']);
  assert.equal(items[1].disabled, true, 'the first group cannot move up');
  items[0].click();
  await waitFor(() => L.getLayout().groups[0].label === 'Daily', { what: 'the rename' });
  assert.equal(document.querySelector('#work-section .nav-group-title').textContent, 'Daily');
  assert.equal(document.getElementById('nav-group-menu'), null, 'choosing an entry closes the menu');
});

test('the group menu moves a group down', async () => {
  document.querySelector('#work-section .nav-group-menu-btn').click();
  Array.from(document.querySelectorAll('#nav-group-menu [role="menuitem"]'))[2].click();
  assert.deepEqual(groupIds(document), ['agents', 'work', 'more']);
});

test('folding a group is kept in the layout and the group body hides', () => {
  document.querySelector('#agents-section .nav-group-title').click();
  assert.equal(document.getElementById('agents-section').classList.contains('collapsed'), true);
  assert.equal(L.getLayout().groups.find((g) => g.id === 'agents').collapsed, true);
  assert.equal(document.querySelector('#agents-section .nav-group-toggle').getAttribute('aria-expanded'), 'false');
  document.querySelector('#agents-section .nav-group-title').click();
  assert.equal(L.getLayout().groups.find((g) => g.id === 'agents').collapsed, false);
});

test('deleting a group moves its rows to the target and retires the container', () => {
  L.updateLayout((layout) => L.deleteGroup(layout, 'agents', 'more'));
  assert.deepEqual(groupIds(document), ['work', 'more']);
  assert.deepEqual(rowIds(document, 'more').slice(-2), ['tool-agents-btn', 'tool-workbench-btn']);
  // Reset brings the container back with its rows.
  L.resetLayout();
  assert.deepEqual(groupIds(document), ['work', 'agents', 'more']);
  assert.deepEqual(rowIds(document, 'agents'), ['tool-agents-btn', 'tool-workbench-btn']);
});

test('a tool the account cannot see stays hidden and keeps its saved place', () => {
  byId('tool-lotus-btn').style.display = 'none';
  L.updateLayout((layout) => L.moveItem(layout, 'gallery', 'work'));
  assert.ok(L.getLayout().groups.find((g) => g.id === 'more').items.includes('lotus'));
  assert.equal(byId('tool-lotus-btn').style.display, 'none');
  byId('tool-lotus-btn').style.display = '';
});

test('an id the build does not know is kept in the layout and draws nothing', () => {
  const layout = JSON.parse(JSON.stringify(L.getLayout()));
  layout.groups[0].items.push('future-tool');
  L.writeLayout(layout);
  assert.ok(L.getLayout().groups[0].items.includes('future-tool'));
  assert.deepEqual(rowIds(document, 'work').length, 5);
});

test('dragging a tool onto another row of its group drops it before that row, with a line shown', () => {
  const work = byId('work-section');
  layoutRows(work);
  const tasks = byId('tool-tasks-btn');
  fire(tasks, 'dragstart');
  const over = fire(byId('tool-today-btn'), 'dragover', { clientY: 5 });
  assert.equal(over.defaultPrevented, true, 'the row accepts the drop');
  assert.ok(byId('tool-today-btn').classList.contains('nav-drop-before'), 'an insertion line shows above Today');
  fire(byId('tool-today-btn'), 'drop', { clientY: 5 });
  assert.deepEqual(rowIds(document, 'work')[0], 'tool-tasks-btn');
  assert.equal(document.querySelector('.nav-drop-before, .nav-drop-after'), null, 'the line is cleared');
});

test('a tool can be dropped on an empty group and does not launch afterwards', async () => {
  D.setDialogs({ styledPrompt: async () => 'Clients' });
  const id = await nav.promptNewGroup();
  const empty = document.querySelector(`.nav-group[data-nav-group="${id}"]`);
  const notes = byId('tool-notes-btn');
  fire(notes, 'dragstart');
  const over = fire(empty.querySelector('.nav-group-empty'), 'dragover', { clientY: 0 });
  assert.equal(over.defaultPrevented, true, 'an empty group is a drop zone');
  assert.ok(empty.classList.contains('nav-drop-into'));
  fire(empty.querySelector('.nav-group-empty'), 'drop', { clientY: 0 });
  fire(notes, 'dragend');
  assert.deepEqual(rowIds(document, id), ['tool-notes-btn']);
  assert.equal(empty.dataset.empty, 'false');
  // The click some browsers send after a drop must not open the tool.
  notes.click();
  assert.deepEqual(clicks, []);
  await sleep(350);
  notes.click();
  assert.deepEqual(clicks, ['tool-notes-btn']);
});

test('dragging a group header onto another group reorders the groups', () => {
  for (const id of ['work', 'more']) {
    const header = byId(`${id}-section`).querySelector('.nav-group-header');
    header.getBoundingClientRect = () => ({ top: 0, height: 26, bottom: 26, left: 0, right: 200, width: 200 });
  }
  fire(byId('more-section').querySelector('.nav-group-header'), 'dragstart');
  fire(byId('work-section'), 'dragover', { clientY: 2 });
  assert.ok(byId('work-section').classList.contains('nav-drop-before'));
  fire(byId('work-section'), 'drop', { clientY: 2 });
  assert.deepEqual(groupIds(document), ['more', 'work', 'agents']);
});

test('Alt+ArrowDown on a focused row moves it down one place', () => {
  const event = new window.KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true, bubbles: true, cancelable: true });
  byId('tool-today-btn').dispatchEvent(event);
  assert.deepEqual(rowIds(document, 'work').slice(0, 2), ['tool-library-btn', 'tool-today-btn']);
});

test('the Command menu is gone: no rail trigger, no sidebar menu', () => {
  assert.equal(document.getElementById('rail-command'), null);
  assert.equal(document.getElementById('sidebar-command-menu'), null);
  assert.equal(L.NAV_ITEMS.some((item) => item.key === 'command'), false);
});
