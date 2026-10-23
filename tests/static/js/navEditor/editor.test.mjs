// "Edit navigation": every edit reachable without a pointer. Move up / Move
// down for groups and tools, Move to... for a tool (including into an empty
// group), rename, icon, new group, delete with a place for its tools, reset.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';
import { buildNavDom, groupIds, rowIds } from '../_support/navFixture.mjs';

const dom = installDom();
after(() => dom.restore());
const fake = installFetchFake();
fake.route('GET', /^\/api\/prefs\//, () => ({ value: null }));
fake.route('PUT', /^\/api\/prefs\//, () => jsonResponse({ ok: true }));
buildNavDom();
await import('../../../../static/js/navOrder.js');
const L = await import('../../../../static/js/navLayout.js');
const D = await import('../../../../static/js/navDom.js');
const editor = await import('../../../../static/js/navEditor.js');

const sleep = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const root = () => document.getElementById('nav-editor');
const ids = (groupId) => L.getLayout().groups.find((g) => g.id === groupId).items;
const q = (selector) => root().querySelector(selector);
const act = (name, attrs = {}) => Array.from(root().querySelectorAll(`[data-act="${name}"]`))
  .find((node) => Object.entries(attrs).every(([k, v]) => node.getAttribute(`data-${k}`) === v));
const choose = (select, value) => { select.value = value; select.dispatchEvent(new window.Event('change', { bubbles: true })); };
const key = (type, init) => new window.KeyboardEvent(type, { bubbles: true, cancelable: true, ...init });

let opener;
beforeEach(async () => {
  editor.closeNavEditor();
  D.setDialogs({ styledConfirm: async () => true, styledPrompt: async () => null });
  L.resetLayout();
  await sleep();
  opener = document.getElementById('rail-settings');
  opener.focus();
});

test('opens as a modal dialog with every group and tool, focus on the first group name', () => {
  editor.openNavEditor();
  assert.equal(q('[role="dialog"]').getAttribute('aria-modal'), 'true');
  assert.equal(q('#nav-editor-title').textContent, 'Edit navigation');
  assert.deepEqual(Array.from(root().querySelectorAll('.nav-editor-name')).map((i) => i.value), ['Work', 'Agents', 'More']);
  assert.equal(root().querySelectorAll('.nav-editor-item').length, 14);
  assert.equal(document.activeElement, q('.nav-editor-name'));
});

test('Move up and Move down reorder a tool inside its group and keep focus on the button', () => {
  editor.openNavEditor();
  act('item-down', { item: 'today' }).click();
  assert.deepEqual(ids('work').slice(0, 2), ['email', 'today']);
  assert.deepEqual(rowIds(document, 'work').slice(0, 2), ['tool-today-btn', 'tool-library-btn'], 'Email has no sidebar row; the others keep their order');
  assert.equal(document.activeElement, act('item-down', { item: 'today' }));
  act('item-up', { item: 'today' }).click();
  assert.deepEqual(ids('work').slice(0, 2), ['today', 'email']);
  assert.equal(act('item-up', { item: 'today' }).disabled, true, 'the first tool cannot move up');
  assert.equal(act('item-down', { item: 'calendar' }).disabled, true, 'the last tool cannot move down');
});

test('Move to... sends a tool to another group and the sidebar follows', () => {
  editor.openNavEditor();
  choose(act('item-move', { item: 'tasks' }), 'agents');
  assert.deepEqual(ids('agents'), ['agents', 'workbench', 'tasks']);
  assert.deepEqual(rowIds(document, 'agents'), ['tool-agents-btn', 'tool-workbench-btn', 'tool-tasks-btn']);
  assert.ok(root().querySelector('.nav-editor-group[data-group="agents"] [data-item="tasks"]'), 'the dialog redraws with Tasks under Agents');
  assert.match(q('.nav-editor-status').textContent, /Moved Tasks to Agents/);
});

test('New group adds an empty group with its name selected, and a tool can be moved into it', () => {
  editor.openNavEditor();
  act('new-group').click();
  const added = L.getLayout().groups.at(-1);
  assert.equal(added.label, 'New group');
  assert.equal(added.items.length, 0);
  assert.equal(document.activeElement.value, 'New group');
  choose(document.activeElement, 'Clients');
  assert.equal(L.getLayout().groups.at(-1).label, 'Clients');
  choose(act('item-move', { item: 'notes' }), added.id);
  assert.deepEqual(ids(added.id), ['notes']);
  assert.deepEqual(rowIds(document, added.id), ['tool-notes-btn']);
});

test('Rename saves on change and an empty name is put back', () => {
  editor.openNavEditor();
  choose(act('group-name', { group: 'work' }), '  Daily  ');
  assert.equal(L.getLayout().groups[0].label, 'Daily');
  choose(act('group-name', { group: 'work' }), '   ');
  assert.equal(L.getLayout().groups[0].label, 'Daily');
  assert.equal(act('group-name', { group: 'work' }).value, 'Daily');
});

test('groups move up and down and take an icon', () => {
  editor.openNavEditor();
  act('group-down', { group: 'work' }).click();
  assert.deepEqual(groupIds(document), ['agents', 'work', 'more']);
  assert.equal(act('group-up', { group: 'agents' }).disabled, true);
  choose(act('group-icon', { group: 'more' }), 'star');
  assert.equal(L.getLayout().groups.find((g) => g.id === 'more').icon, 'star');
});

test('deleting a group asks where its tools go, defaults to the first remaining group, and loses no tool', () => {
  editor.openNavEditor();
  const before = L.getLayout().groups.flatMap((g) => g.items).sort();
  act('group-delete', { group: 'work' }).click();
  const target = q('[data-act="delete-target"]');
  assert.deepEqual(Array.from(target.options).map((o) => o.value), ['agents', 'more']);
  assert.equal(target.value, 'agents', 'the first remaining group is the default');
  assert.deepEqual(groupIds(document), ['work', 'agents', 'more'], 'nothing is deleted before confirming');
  choose(target, 'more');
  act('delete-confirm').click();
  assert.deepEqual(groupIds(document), ['agents', 'more']);
  assert.deepEqual(L.getLayout().groups.flatMap((g) => g.items).sort(), before);
  assert.deepEqual(ids('more').slice(-6), ['today', 'email', 'library', 'notes', 'tasks', 'calendar']);
});

test('Delete is unavailable for the last group, Escape backs out of the delete step before closing', () => {
  L.writeLayout(L.deleteGroup(L.deleteGroup(L.getLayout(), 'agents'), 'more'));
  editor.openNavEditor();
  assert.equal(act('group-delete', { group: 'work' }).disabled, true);
  editor.closeNavEditor();
  L.resetLayout();
  editor.openNavEditor({ group: 'more', deleting: true });
  assert.ok(q('[data-act="delete-target"]'), 'the group menu can open the dialog on its delete step');
  root().dispatchEvent(key('keydown', { key: 'Escape' }));
  assert.equal(q('[data-act="delete-target"]'), null);
  assert.ok(root(), 'the first Escape only cancels the delete step');
  root().dispatchEvent(key('keydown', { key: 'Escape' }));
  assert.equal(root(), null);
  assert.equal(document.activeElement, opener, 'focus goes back to where it was');
});

test('Reset asks first and restores the default layout; declining changes nothing', async () => {
  L.updateLayout((layout) => L.createGroup(L.moveItem(layout, 'notes', 'more'), { label: 'Clients' }).layout);
  editor.openNavEditor();
  let asked = null;
  D.setDialogs({ styledConfirm: async (message, options) => { asked = options; return false; } });
  act('reset').click();
  await sleep();
  assert.equal(asked.confirmText, 'Reset');
  assert.equal(L.getLayout().groups.length, 4, 'declined: unchanged');
  D.setDialogs({ styledConfirm: async () => true });
  act('reset').click();
  await sleep();
  assert.deepEqual(L.getLayout(), L.defaultLayout());
  assert.deepEqual(groupIds(document), ['work', 'agents', 'more']);
});

test('a tool the account cannot see is not listed, and editing around it keeps its place', () => {
  document.getElementById('tool-lotus-btn').style.display = 'none';
  editor.openNavEditor();
  assert.equal(root().querySelector('[data-item="lotus"]'), null);
  choose(act('item-move', { item: 'gallery' }), 'work');
  assert.ok(ids('more').includes('lotus'));
  document.getElementById('tool-lotus-btn').style.display = '';
});

test('Tab stays inside the dialog', () => {
  editor.openNavEditor();
  const focusable = Array.from(root().querySelectorAll('button, input, select')).filter((node) => !node.disabled);
  focusable.at(-1).focus();
  const event = key('keydown', { key: 'Tab' });
  focusable.at(-1).dispatchEvent(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(document.activeElement, focusable[0]);
});

test('the group menu opens the dialog on that group', async () => {
  document.querySelector('#more-section .nav-group-menu-btn').click();
  Array.from(document.querySelectorAll('#nav-group-menu [role="menuitem"]')).find((n) => n.textContent === 'Edit navigation…').click();
  assert.ok(root());
  assert.equal(document.activeElement.dataset.group, 'more');
});
