// The collapsed rail: one icon per navigation group in saved group order, and a
// labeled flyout beside the rail for the group's tools. The sidebar is never
// expanded; a flyout row clicks the tool's original node.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';
import { buildNavDom } from '../_support/navFixture.mjs';

const dom = installDom();
after(() => dom.restore());
const fake = installFetchFake();
fake.route('GET', /^\/api\/prefs\//, () => ({ value: null }));
fake.route('PUT', /^\/api\/prefs\//, () => jsonResponse({ ok: true }));
const { clicks } = buildNavDom();
await import('../../../../static/js/navOrder.js');
const L = await import('../../../../static/js/navLayout.js');
const rail = await import('../../../../static/js/navRail.js');
const D = await import('../../../../static/js/navDom.js');

const sleep = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const byId = (id) => document.getElementById(id);
const button = (group) => byId(`navgrp-${group}`);
const flyout = () => byId('nav-flyout');
const shownFlyout = () => (flyout() && !flyout().hidden ? flyout() : null);
const labels = () => Array.from(flyout().querySelectorAll('.nav-flyout-item')).map((row) => row.dataset.navItem);
const key = (type, init) => new window.KeyboardEvent(type, { bubbles: true, cancelable: true, ...init });
const rect = (left, top, width, height) => ({ left, top, width, height, right: left + width, bottom: top + height });

byId('icon-rail').getBoundingClientRect = () => rect(0, 0, 48, 900);

beforeEach(async () => {
  rail.closeFlyout(document, { restoreFocus: false });
  document.defaultView.matchMedia = () => ({ matches: false });
  for (const id of ['assistant-notif-dot', 'tool-agents-dot', 'email-unread-dot']) byId(id).style.display = 'none';
  byId('tool-lotus-btn').style.display = '';
  clicks.length = 0;
  L.resetLayout();
  await sleep();
  for (const id of ['work', 'agents', 'more']) if (button(id)) button(id).getBoundingClientRect = () => rect(4, 100, 34, 34);
});

test('the rail shows one icon per group in saved order, each named and with a tooltip', async () => {
  assert.deepEqual(Array.from(byId('rail-nav-groups').children).map((node) => node.dataset.navGroupBtn), ['work', 'agents', 'more']);
  for (const [id, label] of [['work', 'Work'], ['agents', 'Agents'], ['more', 'More']]) {
    assert.equal(button(id).getAttribute('aria-label'), label);
    assert.equal(button(id).title, label);
  }
  L.updateLayout((layout) => L.moveGroupBy(L.moveGroupBy(layout, 'more', -1), 'more', -1));
  await sleep();
  assert.deepEqual(Array.from(byId('rail-nav-groups').children).map((node) => node.dataset.navGroupBtn), ['more', 'work', 'agents']);
});

test('a created group gets a first-letter icon until it picks one, and a built-in group has an svg', async () => {
  const { layout, id } = L.createGroup(L.defaultLayout(), { label: 'clients' });
  L.writeLayout(L.moveItem(layout, 'notes', id));
  await sleep();
  assert.equal(button(id).querySelector('.nav-mono').textContent, 'C');
  assert.equal(button(id).getAttribute('aria-label'), 'clients');
  assert.ok(button('work').querySelector('svg'));
  L.updateLayout((current) => L.setGroupIcon(current, id, 'star'));
  await sleep();
  assert.ok(button(id).querySelector('svg') && !button(id).querySelector('.nav-mono'));
});

test('the rail keeps its own per-tool buttons out of sight and leaves the fixed actions alone', () => {
  assert.equal(byId('rail-tasks').classList.contains('nav-rail-tool'), true);
  assert.equal(byId('rail-settings').classList.contains('nav-rail-tool'), false);
  assert.equal(byId('rail-search-btn').classList.contains('nav-rail-tool'), false);
});

test('activating a group icon opens a labeled flyout of its tools in saved order, beside the rail', () => {
  L.updateLayout((layout) => L.moveItem(layout, 'tasks', 'work', 'today'));
  button('work').click();
  const el = shownFlyout();
  assert.ok(el, 'the flyout is open');
  assert.equal(el.getAttribute('aria-label'), 'Work navigation');
  assert.equal(el.querySelector('.nav-flyout-title').textContent, 'Work');
  assert.deepEqual(labels(), ['tasks', 'today', 'email', 'library', 'notes', 'calendar']);
  assert.equal(button('work').getAttribute('aria-expanded'), 'true');
  assert.equal(el.style.left, '54px', 'anchored just right of the 48px rail');
  assert.equal(el.style.top, '100px', 'level with its icon');
  assert.equal(byId('sidebar').classList.contains('hidden'), false);
});

test('a flyout row shows the tool name and icon and runs the original node', () => {
  button('agents').click();
  const row = flyout().querySelector('[data-nav-item="workbench"]');
  assert.equal(row.querySelector('.nav-flyout-label').textContent, 'Workbench');
  assert.ok(row.querySelector('svg'));
  row.click();
  assert.deepEqual(clicks, ['tool-workbench-btn']);
  assert.equal(shownFlyout(), null, 'choosing a tool closes the flyout');
});

test('Email, which has only a rail button, still opens from its group flyout', () => {
  button('work').click();
  flyout().querySelector('[data-nav-item="email"]').click();
  assert.deepEqual(clicks, ['rail-email']);
});

test('Escape closes the flyout and returns focus to the group icon, which stays', () => {
  button('more').click();
  assert.ok(shownFlyout());
  flyout().querySelector('.nav-flyout-item').focus();
  document.activeElement.dispatchEvent(key('keydown', { key: 'Escape' }));
  assert.equal(shownFlyout(), null);
  assert.equal(document.activeElement, button('more'));
  assert.ok(button('more').isConnected);
  assert.equal(button('more').getAttribute('aria-expanded'), 'false');
});

test('a click outside closes the flyout; a click inside does not', () => {
  button('work').click();
  flyout().dispatchEvent(new window.Event('pointerdown', { bubbles: true }));
  assert.ok(shownFlyout());
  document.body.dispatchEvent(new window.Event('pointerdown', { bubbles: true }));
  assert.equal(shownFlyout(), null);
});

test('only one flyout is open: opening another group replaces it, and the same icon again closes it', () => {
  button('work').click();
  button('more').click();
  assert.equal(document.querySelectorAll('.nav-flyout:not([hidden])').length, 1);
  assert.equal(flyout().querySelector('.nav-flyout-title').textContent, 'More');
  assert.equal(button('work').getAttribute('aria-expanded'), 'false');
  button('more').click();
  assert.equal(shownFlyout(), null);
});

test('arrow keys move through the flyout items and wrap, Home and End jump', () => {
  button('agents').click();
  const rows = Array.from(flyout().querySelectorAll('.nav-flyout-item'));
  rows[0].focus();
  rows[0].dispatchEvent(key('keydown', { key: 'ArrowDown' }));
  assert.equal(document.activeElement, rows[1]);
  rows[1].dispatchEvent(key('keydown', { key: 'ArrowDown' }));
  assert.equal(document.activeElement, rows[0], 'wraps past the last');
  rows[0].dispatchEvent(key('keydown', { key: 'ArrowUp' }));
  assert.equal(document.activeElement, rows[1], 'wraps before the first');
  rows[1].dispatchEvent(key('keydown', { key: 'Home' }));
  assert.equal(document.activeElement, rows[0]);
  rows[0].dispatchEvent(key('keydown', { key: 'End' }));
  assert.equal(document.activeElement, rows[1]);
});

test('ArrowDown on a group icon opens its flyout on the first tool', () => {
  button('agents').focus();
  button('agents').dispatchEvent(key('keydown', { key: 'ArrowDown' }));
  assert.ok(shownFlyout());
  assert.equal(document.activeElement, flyout().querySelector('.nav-flyout-item'));
});

test('a group with many tools stays inside the viewport and scrolls in its list', () => {
  button('more').click();
  const view = document.defaultView.innerHeight;
  assert.equal(flyout().style.maxHeight, `${view - 16}px`);
  assert.ok(flyout().querySelector('.nav-flyout-list'), 'the tools sit in their own list, which scrolls');
});

test('on a narrow screen the flyout is a sheet that fits the viewport, not anchored to the rail', () => {
  document.defaultView.matchMedia = () => ({ matches: true });
  button('work').click();
  assert.equal(flyout().classList.contains('nav-flyout-sheet'), true);
  assert.equal(flyout().style.left, '');
  assert.equal(flyout().style.top, '');
  flyout().querySelector('.nav-flyout-close').click();
  assert.equal(shownFlyout(), null, 'the sheet has its own close control');
});

test('a tool hidden for this account is not listed, and a group with none shown has no icon', async () => {
  byId('tool-lotus-btn').style.display = 'none';
  button('more').click();
  assert.ok(!labels().includes('lotus'));
  rail.closeFlyout(document, { restoreFocus: false });
  byId('tool-agents-btn').style.display = 'none';
  byId('tool-workbench-btn').style.display = 'none';
  await sleep();
  assert.equal(button('agents'), null);
  assert.ok(L.getLayout().groups.find((g) => g.id === 'agents').items.includes('agents'), 'placement is kept');
  byId('tool-agents-btn').style.display = '';
  byId('tool-workbench-btn').style.display = '';
  await sleep();
  assert.ok(button('agents'));
});

test('a group icon shows a dot while one of its tools shows one: Tasks and Email in Work', async () => {
  const dot = (id) => button(id).querySelector('.nav-rail-dot');
  assert.equal(dot('work').hidden, true);
  byId('assistant-notif-dot').style.display = 'inline-block';
  await sleep();
  assert.equal(dot('work').hidden, false);
  byId('assistant-notif-dot').style.display = 'none';
  byId('email-unread-dot').style.display = 'inline-block';
  await sleep();
  assert.equal(dot('work').hidden, false, 'the Email unread dot counts, though it sits outside the tool row');
  byId('email-unread-dot').style.display = 'none';
  await sleep();
  assert.equal(dot('work').hidden, true);
  assert.equal(dot('agents').hidden, true);
});

test('the Phalanx badge stays visible on the rail through the Agents icon', async () => {
  const badge = document.createElement('span');
  badge.className = 'rail-agents-badge';
  badge.textContent = '3';
  byId('rail-agents').appendChild(badge);
  await sleep();
  const shown = button('agents').querySelector('.nav-rail-badge');
  assert.equal(shown.hidden, false);
  assert.equal(shown.textContent, '3');
  badge.hidden = true;
  await sleep();
  assert.equal(button('agents').querySelector('.nav-rail-badge').hidden, true);
  badge.remove();
});

test('a tool with a dot shows it in the open flyout, and the flyout follows layout edits', async () => {
  byId('assistant-notif-dot').style.display = 'inline-block';
  button('work').click();
  assert.equal(flyout().querySelector('[data-nav-item="tasks"] .nav-flyout-dot').hidden, false);
  L.updateLayout((layout) => L.moveItem(layout, 'tasks', 'agents'));
  await sleep();
  assert.ok(!labels().includes('tasks'));
});

test('the Command menu is gone from the rail and labels use the page style', () => {
  assert.equal(byId('rail-command'), null);
  document.documentElement.dataset.style = 'agamemnon';
  assert.equal(D.itemLabel('agents', document), 'Phalanx');
  document.documentElement.dataset.style = 'classic';
  assert.equal(D.itemLabel('agents', document), 'Agents');
});
