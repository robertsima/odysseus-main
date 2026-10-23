// The collapsed (mini) rail: the fixed actions, then one icon per navigation
// group in the saved group order. Activating a group icon opens a labeled
// flyout beside the rail listing that group's tools in saved order, without
// expanding the sidebar. Choosing a tool opens it and closes the flyout; Escape
// or a click outside closes it and puts focus back on the group icon.
//
// The rail's own per-tool buttons stay in the page (their listeners and
// indicator classes are owned by feature modules) but are not drawn; a flyout
// row clicks the tool's original node. Only one flyout is open at a time.

import { NAV_ITEMS, getLayout, subscribe } from './navLayout.js';
import {
  groupIconHtml, groupSignal, isGroupContainerHidden, isItemShown, itemIcon, itemLabel, itemSignal,
  launcherNode, railNode, roveFocus,
} from './navDom.js';

const FLYOUT_ID = 'nav-flyout';
const GAP = 6;
const NARROW = '(max-width: 768px)';

let _open = null; // { id, button }
let _renderQueued = false;
let _sidebarMode = '';
let _observer = null;

function containerFor(doc) {
  const rail = doc.getElementById('icon-rail');
  if (!rail) return null;
  let box = doc.getElementById('rail-nav-groups');
  if (!box) {
    box = doc.createElement('div');
    box.id = 'rail-nav-groups';
    box.className = 'rail-nav-groups';
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', 'Navigation groups');
    const before = rail.querySelector('.rail-dynamic') || rail.querySelector('[style*="flex:1"]');
    rail.insertBefore(box, before || null);
  }
  return box;
}

function groupButtons(box) {
  return Array.from(box.querySelectorAll(':scope > [data-nav-group-btn]'));
}

function shownItems(group, doc) {
  return group.items.filter((key) => isItemShown(key, doc));
}

function groupVisible(group, doc) {
  const container = Array.from(doc.querySelectorAll('.nav-group[data-nav-group]')).find((el) => el.dataset.navGroup === group.id);
  if (container && isGroupContainerHidden(container)) return false;
  return shownItems(group, doc).length > 0;
}

function buildButton(group, doc) {
  const button = doc.createElement('button');
  button.type = 'button';
  button.className = 'icon-rail-btn nav-rail-group';
  button.id = `navgrp-${group.id}`;
  button.dataset.navGroupBtn = group.id;
  button.setAttribute('aria-haspopup', 'true');
  button.setAttribute('aria-expanded', 'false');
  button.setAttribute('aria-controls', FLYOUT_ID);
  button.innerHTML = '<span class="nav-rail-icon"></span><span class="nav-rail-dot" hidden></span><span class="nav-rail-badge" hidden></span>';
  return button;
}

function updateButton(button, group, doc) {
  const sig = `${group.icon}|${group.label}`;
  if (button.dataset.sig !== sig) {
    button.dataset.sig = sig;
    button.querySelector('.nav-rail-icon').innerHTML = groupIconHtml(group);
    button.setAttribute('aria-label', group.label);
    button.title = group.label;
  }
  const { active, badge } = groupSignal(group, doc);
  const dot = button.querySelector('.nav-rail-dot');
  const count = button.querySelector('.nav-rail-badge');
  if (dot.hidden !== !(active && !badge)) dot.hidden = !(active && !badge);
  if (count.hidden !== !badge) count.hidden = !badge;
  if (count.textContent !== badge) count.textContent = badge;
  if (button.classList.contains('has-activity') !== active) button.classList.toggle('has-activity', active);
}

/** Bring the rail's group icons in line with the layout and with what the
 *  account can see. Updates buttons in place so focus survives. */
export function renderRail(doc = document) {
  const box = containerFor(doc);
  if (!box) return;
  const shown = getLayout().groups.filter((group) => groupVisible(group, doc));
  const have = new Map(groupButtons(box).map((button) => [button.dataset.navGroupBtn, button]));
  for (const [id, button] of have) {
    if (!shown.some((group) => group.id === id)) {
      if (_open?.id === id) closeFlyout(doc, { restoreFocus: false });
      button.remove();
      have.delete(id);
    }
  }
  let ref = null;
  for (let i = shown.length - 1; i >= 0; i -= 1) {
    const group = shown[i];
    let button = have.get(group.id);
    if (!button) button = buildButton(group, doc);
    updateButton(button, group, doc);
    if (button.parentNode !== box || button.nextSibling !== ref) box.insertBefore(button, ref);
    ref = button;
  }
  if (_open) {
    const group = shown.find((entry) => entry.id === _open.id);
    if (group) fillFlyout(doc, group); else closeFlyout(doc, { restoreFocus: false });
  }
}

function queueRender(doc) {
  if (_renderQueued) return;
  _renderQueued = true;
  Promise.resolve().then(() => {
    _renderQueued = false;
    renderRail(doc);
    // Changes the render itself made are not news.
    _observer?.takeRecords();
  });
}

// ── Flyout ───────────────────────────────────────────────────────────────────

function flyoutEl(doc) {
  let el = doc.getElementById(FLYOUT_ID);
  if (el) return el;
  el = doc.createElement('div');
  el.id = FLYOUT_ID;
  el.className = 'nav-flyout';
  el.setAttribute('role', 'dialog');
  el.hidden = true;
  el.innerHTML = '<div class="nav-flyout-head"><span class="nav-flyout-title"></span>'
    + '<button type="button" class="nav-flyout-close" aria-label="Close">&times;</button></div>'
    + '<div class="nav-flyout-list" role="menu"></div>';
  // Taps inside the flyout must not reach the page's "tap outside the sidebar
  // closes it" handlers; the row's own click on the tool still does.
  el.addEventListener('click', (event) => {
    event.stopPropagation();
    if (event.target.closest('.nav-flyout-close')) { closeFlyout(doc); return; }
    const row = event.target.closest('.nav-flyout-item');
    if (row) chooseItem(row.dataset.navItem, doc);
  });
  el.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') return;
    roveFocus(event, Array.from(el.querySelectorAll('.nav-flyout-item')));
  });
  doc.body.appendChild(el);
  return el;
}

function fillFlyout(doc, group) {
  const el = flyoutEl(doc);
  el.setAttribute('aria-label', `${group.label} navigation`);
  el.querySelector('.nav-flyout-title').textContent = group.label;
  const list = el.querySelector('.nav-flyout-list');
  list.setAttribute('aria-label', group.label);
  const wanted = shownItems(group, doc);
  const focusedKey = doc.activeElement?.dataset?.navItem;
  list.textContent = '';
  for (const key of wanted) {
    const row = doc.createElement('button');
    row.type = 'button';
    row.className = 'nav-flyout-item';
    row.setAttribute('role', 'menuitem');
    row.dataset.navItem = key;
    const icon = itemIcon(key, doc);
    if (icon) row.appendChild(icon);
    const label = doc.createElement('span');
    label.className = 'nav-flyout-label';
    label.textContent = itemLabel(key, doc);
    row.appendChild(label);
    const dot = doc.createElement('span');
    dot.className = 'nav-flyout-dot';
    dot.hidden = !itemSignal(key, doc).active;
    row.appendChild(dot);
    list.appendChild(row);
    if (key === focusedKey) row.focus();
  }
}

function place(doc, el, button) {
  const win = doc.defaultView || window;
  const narrow = win.matchMedia ? win.matchMedia(NARROW).matches : win.innerWidth <= 768;
  el.classList.toggle('nav-flyout-sheet', narrow);
  el.style.top = el.style.left = el.style.right = el.style.maxHeight = '';
  if (narrow) return;
  const rail = button.closest('#icon-rail') || button;
  const railBox = rail.getBoundingClientRect();
  const box = button.getBoundingClientRect();
  const onRight = railBox.left > win.innerWidth / 2;
  if (onRight) el.style.right = `${Math.max(0, win.innerWidth - railBox.left + GAP)}px`;
  else el.style.left = `${railBox.right + GAP}px`;
  el.style.maxHeight = `${Math.max(120, win.innerHeight - 16)}px`;
  const height = el.offsetHeight || 0;
  el.style.top = `${Math.max(8, Math.min(box.top, win.innerHeight - height - 8))}px`;
}

function onOutside(event) {
  const doc = event.target.ownerDocument;
  const el = doc.getElementById(FLYOUT_ID);
  if (el?.contains(event.target) || event.target.closest?.('[data-nav-group-btn]')) return;
  closeFlyout(doc, { restoreFocus: false });
}

function onKey(event) {
  if (event.key !== 'Escape' || !_open) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  closeFlyout(event.target.ownerDocument || document);
}

function onFocusOut(event) {
  const doc = event.target.ownerDocument;
  const next = event.relatedTarget;
  if (!_open || !next) return;
  if (doc.getElementById(FLYOUT_ID)?.contains(next) || next.closest?.('[data-nav-group-btn]')) return;
  closeFlyout(doc, { restoreFocus: false });
}

export function openFlyout(id, doc = document, { focusFirst = false } = {}) {
  const group = getLayout().groups.find((entry) => entry.id === id);
  const button = Array.from(doc.querySelectorAll('[data-nav-group-btn]')).find((node) => node.dataset.navGroupBtn === id);
  if (!group || !button) return null;
  if (_open) closeFlyout(doc, { restoreFocus: false });
  const el = flyoutEl(doc);
  fillFlyout(doc, group);
  el.dataset.group = id;
  el.hidden = false;
  place(doc, el, button);
  button.setAttribute('aria-expanded', 'true');
  _open = { id, button };
  doc.addEventListener('pointerdown', onOutside, true);
  doc.addEventListener('keydown', onKey, true);
  el.addEventListener('focusout', onFocusOut);
  if (focusFirst) el.querySelector('.nav-flyout-item')?.focus();
  return el;
}

export function closeFlyout(doc = document, { restoreFocus = true } = {}) {
  const el = doc.getElementById(FLYOUT_ID);
  const open = _open;
  _open = null;
  doc.removeEventListener('pointerdown', onOutside, true);
  doc.removeEventListener('keydown', onKey, true);
  if (el) {
    el.removeEventListener('focusout', onFocusOut);
    el.hidden = true;
    delete el.dataset.group;
  }
  if (open) {
    open.button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) open.button.focus?.();
  }
}

export function openFlyoutId() { return _open?.id || null; }

function chooseItem(key, doc) {
  const node = launcherNode(key, doc);
  closeFlyout(doc);
  node?.click();
}

// ── Setup ────────────────────────────────────────────────────────────────────

function markRailTools(doc) {
  for (const item of NAV_ITEMS) railNode(item.key, doc)?.classList.add('nav-rail-tool');
}

export function initNavRail(doc = document) {
  const box = containerFor(doc);
  if (!box) return;
  markRailTools(doc);
  box.addEventListener('click', (event) => {
    const button = event.target.closest('[data-nav-group-btn]');
    if (!button) return;
    event.stopPropagation();
    const id = button.dataset.navGroupBtn;
    if (_open?.id === id) { closeFlyout(doc); return; }
    // A click from the keyboard (detail 0) lands on the first tool.
    openFlyout(id, doc, { focusFirst: event.detail === 0 });
  });
  box.addEventListener('keydown', (event) => {
    const button = event.target.closest('[data-nav-group-btn]');
    if (!button) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      event.preventDefault();
      if (_open?.id !== button.dataset.navGroupBtn) openFlyout(button.dataset.navGroupBtn, doc);
      doc.getElementById(FLYOUT_ID)?.querySelector('.nav-flyout-item')?.focus();
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      // Move between group icons.
      const buttons = groupButtons(box);
      const next = buttons[buttons.indexOf(button) + (event.key === 'ArrowUp' ? -1 : 1)];
      if (next) { event.preventDefault(); next.focus(); }
    }
  });
  subscribe(() => queueRender(doc));
  const rerender = () => queueRender(doc);
  if (typeof MutationObserver !== 'undefined') {
    const observer = _observer = new MutationObserver(rerender);
    const options = { subtree: true, childList: true, attributes: true, attributeFilter: ['style', 'class', 'hidden'] };
    for (const root of [doc.getElementById('icon-rail'), doc.querySelector('.sidebar-inner'), doc.getElementById('email-section')]) {
      if (root) observer.observe(root, options);
    }
    // Close the flyout when the sidebar mode changes underneath it.
    _sidebarMode = doc.documentElement.className.match(/ody-sidebar-\w+/g)?.join(' ') || '';
    new MutationObserver(() => {
      const mode = doc.documentElement.className.match(/ody-sidebar-\w+/g)?.join(' ') || '';
      if (mode !== _sidebarMode) { _sidebarMode = mode; closeFlyout(doc, { restoreFocus: false }); }
    }).observe(doc.documentElement, { attributes: true, attributeFilter: ['class'] });
  }
  (doc.defaultView || window).addEventListener('resize', () => closeFlyout(doc, { restoreFocus: false }));
  renderRail(doc);
}

export default { initNavRail, renderRail, openFlyout, closeFlyout, openFlyoutId };
