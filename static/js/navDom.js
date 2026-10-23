// DOM helpers the navigation drawers share: finding a tool's existing node,
// asking whether the account can see it, naming it, and noticing that it has
// something to say (a notification dot). No layout state lives here.

import { GROUP_ICONS, MONOGRAM, itemInfo } from './navLayout.js';

const SVG_ATTRS = 'width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';

function docOf(doc) { return doc || (typeof document !== 'undefined' ? document : null); }

export function sidebarNode(key, doc) {
  const item = itemInfo(key);
  return item?.sidebar ? docOf(doc)?.getElementById(item.sidebar) || null : null;
}

export function railNode(key, doc) {
  const item = itemInfo(key);
  return item?.rail ? docOf(doc)?.getElementById(item.rail) || null : null;
}

/** The node whose click handler opens the tool. */
export function launcherNode(key, doc) {
  const item = itemInfo(key);
  const named = item?.launcher ? docOf(doc)?.getElementById(item.launcher) : null;
  return named || sidebarNode(key, doc) || railNode(key, doc);
}

function hiddenSelf(node) {
  return !node || node.hidden || node.style?.display === 'none' || node.classList?.contains('hidden');
}

/** Shown when its node exists and the account has not hidden it (a privilege
 *  check or Customize UI sets display:none on the node). A key this build does
 *  not know has no node, so it is never shown, yet stays in the stored layout. */
export function isItemShown(key, doc) {
  const item = itemInfo(key);
  if (!item) return false;
  const node = launcherNode(key, doc);
  return !hiddenSelf(node);
}

/** Shown in the sidebar: it has a row there and the account has not hidden it. */
export function isRowShown(key, doc) {
  const node = sidebarNode(key, doc);
  return !!node && !hiddenSelf(node);
}

export function isGroupContainerHidden(node) {
  return !node || node.hidden || node.style?.display === 'none';
}

export function isAgamemnon(doc) {
  return docOf(doc)?.documentElement?.dataset?.style === 'agamemnon';
}

export function itemLabel(key, doc) {
  const item = itemInfo(key);
  if (!item) return key;
  return isAgamemnon(doc) && item.agLabel ? item.agLabel : item.label;
}

/** A copy of the tool's own icon, for a flyout row. */
export function itemIcon(key, doc) {
  const source = (sidebarNode(key, doc) || railNode(key, doc))?.querySelector?.('svg')
    || (railNode(key, doc))?.querySelector?.('svg');
  if (!source) return null;
  const copy = source.cloneNode(true);
  copy.removeAttribute('style');
  copy.setAttribute('width', '16');
  copy.setAttribute('height', '16');
  copy.setAttribute('aria-hidden', 'true');
  copy.setAttribute('focusable', 'false');
  return copy;
}

export function groupIconHtml(group) {
  const icon = GROUP_ICONS[group.icon];
  if (icon) return `<svg ${SVG_ATTRS}>${icon.svg}</svg>`;
  const first = Array.from(String(group.label || '').trim())[0] || '?';
  return `<span class="nav-mono" aria-hidden="true">${escapeHtml(first.toUpperCase())}</span>`;
}

export function groupIconIsMonogram(group) { return !GROUP_ICONS[group.icon]; }
export { MONOGRAM };

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// What counts as "this tool has something to say": a visible notification dot
// or badge inside its sidebar row or rail button, or a flag class the feature
// modules set while a job runs or a window is minimized.
const SIGNAL_CLASSES = ['rail-minimized', 'rail-notify', 'cookbook-notif-active', 'research-notif-active'];
const SIGNAL_DOTS = '.sidebar-notif-dot, .cookbook-notif-dot, .tool-notes-dot, .rail-notes-badge, .rail-agents-badge';
// A dot that lives outside the tool's own row (Email's unread dot sits in the
// Email section header).
const EXTRA_DOTS = { email: ['email-unread-dot'] };

function dotVisible(el) {
  return !!el && !el.hidden && el.style?.display !== 'none';
}

export function itemSignal(key, doc) {
  const d = docOf(doc);
  const nodes = [sidebarNode(key, d), railNode(key, d)].filter(Boolean);
  let active = false;
  let badge = '';
  for (const node of nodes) {
    if (SIGNAL_CLASSES.some((name) => node.classList.contains(name))) active = true;
    for (const dot of node.querySelectorAll(SIGNAL_DOTS)) {
      if (!dotVisible(dot)) continue;
      active = true;
      if (dot.classList.contains('rail-agents-badge') && dot.textContent.trim()) badge = dot.textContent.trim();
    }
  }
  for (const id of EXTRA_DOTS[key] || []) {
    if (dotVisible(d?.getElementById(id))) active = true;
  }
  return { active, badge };
}

/** Roll a group's tools up into one signal (only tools the account sees). */
export function groupSignal(group, doc) {
  let active = false;
  let badge = '';
  for (const key of group.items) {
    if (!isItemShown(key, doc)) continue;
    const signal = itemSignal(key, doc);
    active = active || signal.active;
    badge = badge || signal.badge;
  }
  return { active, badge };
}

// ── Dialogs ──────────────────────────────────────────────────────────────────
// Rename and reset use the app's styled prompt and confirm. ui.js is large, so
// it loads on first use; a test passes its own through setDialogs().
let _dialogs = null;
export function setDialogs(dialogs) { _dialogs = dialogs; }
async function dialogs() { return _dialogs || import('./ui.js'); }
export async function askText(message, options) { return (await dialogs()).styledPrompt(message, options); }
export async function askConfirm(message, options) { return (await dialogs()).styledConfirm(message, options); }

// ── Menus ────────────────────────────────────────────────────────────────────

/** Arrow-key movement across the focusable items of a menu or flyout. Returns
 *  true when it handled the key. */
export function roveFocus(event, items) {
  const list = items.filter((el) => el && !el.disabled);
  if (!list.length) return false;
  const at = list.indexOf(event.target.closest?.('[role="menuitem"], button') || event.target);
  let next = null;
  if (event.key === 'ArrowDown') next = list[(at + 1) % list.length];
  else if (event.key === 'ArrowUp') next = list[(at - 1 + list.length) % list.length];
  else if (event.key === 'Home') next = list[0];
  else if (event.key === 'End') next = list[list.length - 1];
  if (!next) return false;
  event.preventDefault();
  next.focus();
  return true;
}

/** A small popup menu next to `anchor`. `entries` are { label, onSelect,
 *  disabled?, danger? }. Escape or an outside click closes it and puts focus
 *  back on the anchor. */
export function showMenu(anchor, entries, { doc = document, id = 'nav-popup-menu', label = '' } = {}) {
  doc.getElementById(id)?.remove();
  const menu = doc.createElement('div');
  menu.id = id;
  menu.className = 'nav-popup-menu';
  menu.setAttribute('role', 'menu');
  if (label) menu.setAttribute('aria-label', label);
  const buttons = entries.map((entry) => {
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'nav-popup-item' + (entry.danger ? ' danger' : '');
    button.setAttribute('role', 'menuitem');
    button.textContent = entry.label;
    button.disabled = !!entry.disabled;
    button.addEventListener('click', () => { close(false); entry.onSelect?.(); });
    menu.appendChild(button);
    return button;
  });
  const rect = anchor.getBoundingClientRect();
  const view = doc.defaultView || window;
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 2, view.innerHeight - 8 - entries.length * 36))}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.left, view.innerWidth - 220))}px`;
  doc.body.appendChild(menu);
  anchor.setAttribute('aria-expanded', 'true');

  function close(restoreFocus = true) {
    if (!menu.isConnected) return;
    menu.remove();
    anchor.setAttribute('aria-expanded', 'false');
    doc.removeEventListener('pointerdown', onOutside, true);
    doc.removeEventListener('keydown', onKey, true);
    if (restoreFocus) anchor.focus?.();
  }
  function onOutside(event) { if (!menu.contains(event.target) && !anchor.contains(event.target)) close(false); }
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (event.key === 'Tab') { close(false); return; }
    roveFocus(event, buttons);
  }
  doc.addEventListener('pointerdown', onOutside, true);
  doc.addEventListener('keydown', onKey, true);
  buttons.find((b) => !b.disabled)?.focus();
  return { menu, close };
}
