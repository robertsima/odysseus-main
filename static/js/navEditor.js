// "Edit navigation": the keyboard-reachable way to arrange the navigation.
// Every change here is also possible by dragging, but nothing here needs a
// pointer: groups and tools have Move up / Move down, a tool has Move to...,
// groups can be renamed, given an icon, created and deleted, and the whole
// layout can be reset. Changes save as they are made.

import {
  GROUP_ICONS, MONOGRAM, createGroup, deleteGroup, findGroup, getLayout, moveGroupBy, moveItem, moveItemBy,
  renameGroup, resetLayout, setGroupIcon, subscribe, updateLayout,
} from './navLayout.js';
import { askConfirm, groupIconIsMonogram, isItemShown, itemLabel } from './navDom.js';

const DIALOG_ID = 'nav-editor';

let _state = null; // { doc, root, deleting, unsubscribe, opener }

function el(doc, tag, props = {}, children = []) {
  const node = doc.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (value === true) node.setAttribute(key, '');
    else if (value !== false && value != null) node.setAttribute(key, String(value));
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function button(doc, label, act, extra = {}) {
  return el(doc, 'button', { type: 'button', 'data-act': act, ...extra, text: label });
}

function announce(message) {
  const status = _state?.root.querySelector('.nav-editor-status');
  if (status) status.textContent = message;
}

function shownKeys(group, doc) { return group.items.filter((key) => isItemShown(key, doc)); }

function renderGroup(doc, layout, group, index) {
  const others = layout.groups.filter((entry) => entry.id !== group.id);
  const shownGroupItems = shownKeys(group, doc);
  const iconSelect = el(doc, 'select', { class: 'nav-editor-icon', 'data-act': 'group-icon', 'data-group': group.id, 'aria-label': `Icon for ${group.label}` });
  iconSelect.appendChild(el(doc, 'option', { value: MONOGRAM, text: 'First letter' }));
  for (const [id, icon] of Object.entries(GROUP_ICONS)) iconSelect.appendChild(el(doc, 'option', { value: id, text: icon.label }));
  iconSelect.value = groupIconIsMonogram(group) ? MONOGRAM : group.icon;

  const head = el(doc, 'div', { class: 'nav-editor-group-head' }, [
    el(doc, 'input', { type: 'text', class: 'nav-editor-name', 'data-act': 'group-name', 'data-group': group.id, value: group.label, maxlength: 32, 'aria-label': `Name of group ${index + 1}` }),
    iconSelect,
    button(doc, 'Up', 'group-up', { 'data-group': group.id, 'aria-label': `Move ${group.label} up`, disabled: index === 0 }),
    button(doc, 'Down', 'group-down', { 'data-group': group.id, 'aria-label': `Move ${group.label} down`, disabled: index === layout.groups.length - 1 }),
    button(doc, 'Delete', 'group-delete', { 'data-group': group.id, 'aria-label': `Delete ${group.label}`, class: 'danger', disabled: layout.groups.length < 2 }),
  ]);
  head.querySelector('input').value = group.label;

  const section = el(doc, 'section', { class: 'nav-editor-group', 'data-group': group.id, 'aria-label': group.label }, [head]);

  if (_state.deleting === group.id && others.length) {
    const target = el(doc, 'select', { 'data-act': 'delete-target', 'aria-label': 'Move its tools to' });
    for (const other of others) target.appendChild(el(doc, 'option', { value: other.id, text: other.label }));
    const count = group.items.length;
    section.appendChild(el(doc, 'div', { class: 'nav-editor-delete', role: 'group', 'aria-label': `Delete ${group.label}` }, [
      el(doc, 'span', { text: count ? `Move its ${count === 1 ? 'tool' : `${count} tools`} to` : 'This group is empty.' }),
      ...(count ? [target] : []),
      button(doc, 'Delete group', 'delete-confirm', { 'data-group': group.id, class: 'danger' }),
      button(doc, 'Cancel', 'delete-cancel'),
    ]));
  }

  const list = el(doc, 'ul', { class: 'nav-editor-items' });
  for (const key of shownGroupItems) {
    const name = itemLabel(key, doc);
    const mover = el(doc, 'select', { 'data-act': 'item-move', 'data-item': key, 'aria-label': `Move ${name} to` });
    mover.appendChild(el(doc, 'option', { value: '', text: 'Move to…' }));
    for (const other of others) mover.appendChild(el(doc, 'option', { value: other.id, text: other.label }));
    const canUp = moveItemBy(layout, key, -1, (k) => isItemShown(k, doc)) !== layout;
    const canDown = moveItemBy(layout, key, 1, (k) => isItemShown(k, doc)) !== layout;
    list.appendChild(el(doc, 'li', { class: 'nav-editor-item', 'data-item': key }, [
      el(doc, 'span', { class: 'nav-editor-item-name', text: name }),
      button(doc, 'Up', 'item-up', { 'data-item': key, 'aria-label': `Move ${name} up`, disabled: !canUp }),
      button(doc, 'Down', 'item-down', { 'data-item': key, 'aria-label': `Move ${name} down`, disabled: !canDown }),
      mover,
    ]));
  }
  if (!shownGroupItems.length) list.appendChild(el(doc, 'li', { class: 'nav-editor-none', text: 'No tools in this group.' }));
  section.appendChild(list);
  return section;
}

function render(doc, focus) {
  if (!_state) return;
  const body = _state.root.querySelector('.nav-editor-body');
  const scroll = body.scrollTop;
  body.textContent = '';
  const layout = getLayout();
  layout.groups.forEach((group, index) => body.appendChild(renderGroup(doc, layout, group, index)));
  body.scrollTop = scroll;
  if (focus) {
    const target = Array.from(_state.root.querySelectorAll('[data-act]')).find((node) => Object.entries(focus).every(([k, v]) => node.getAttribute(k) === v));
    (target && !target.disabled ? target : _state.root.querySelector('.nav-editor-name'))?.focus();
  }
}

function change(doc, fn, message, focus) {
  updateLayout(fn);
  render(doc, focus);
  if (message) announce(message);
}

function name(layout, id) { return findGroup(layout, id)?.label || ''; }

async function onClick(event, doc) {
  const target = event.target.closest('[data-act]');
  if (!target || target.tagName === 'SELECT' || target.tagName === 'INPUT') return;
  const act = target.dataset.act;
  const id = target.dataset.group;
  const key = target.dataset.item;
  const shown = (k) => isItemShown(k, doc);
  const groupShown = (group) => group.items.length === 0 || group.items.some(shown);
  if (act === 'done') { closeNavEditor(doc); return; }
  if (act === 'new-group') {
    let created = null;
    change(doc, (layout) => { const made = createGroup(layout, { label: 'New group' }); created = made.id; return made.layout; }, 'Added a group.');
    if (created) {
      render(doc, { 'data-act': 'group-name', 'data-group': created });
      _state.root.querySelector(`[data-act="group-name"][data-group="${created}"]`)?.select?.();
    }
    return;
  }
  if (act === 'group-up' || act === 'group-down') {
    const delta = act === 'group-up' ? -1 : 1;
    change(doc, (layout) => moveGroupBy(layout, id, delta, groupShown), `Moved ${name(getLayout(), id)} ${delta < 0 ? 'up' : 'down'}.`, { 'data-act': act, 'data-group': id });
    return;
  }
  if (act === 'item-up' || act === 'item-down') {
    const delta = act === 'item-up' ? -1 : 1;
    change(doc, (layout) => moveItemBy(layout, key, delta, shown), `Moved ${itemLabel(key, doc)} ${delta < 0 ? 'up' : 'down'}.`, { 'data-act': act, 'data-item': key });
    return;
  }
  if (act === 'group-delete') {
    _state.deleting = id;
    render(doc, { 'data-act': 'delete-confirm', 'data-group': id });
    return;
  }
  if (act === 'delete-cancel') {
    const id2 = _state.deleting;
    _state.deleting = null;
    render(doc, { 'data-act': 'group-delete', 'data-group': id2 });
    return;
  }
  if (act === 'delete-confirm') {
    const select = _state.root.querySelector('[data-act="delete-target"]');
    const before = getLayout();
    const label = name(before, id);
    const moveTo = select?.value || undefined;
    _state.deleting = null;
    change(doc, (layout) => deleteGroup(layout, id, moveTo), `Deleted ${label}. Its tools moved to ${name(getLayout(), moveTo || before.groups.find((g) => g.id !== id)?.id)}.`);
    _state.root.querySelector('.nav-editor-name')?.focus();
    return;
  }
  if (act === 'reset') {
    const ok = await askConfirm('Put the groups and tools back in the default arrangement? Your own groups are removed and their tools return to their usual places.', { title: 'Reset navigation', confirmText: 'Reset', danger: true });
    if (ok) { _state.deleting = null; resetLayout(); render(doc, { 'data-act': 'new-group' }); announce('Navigation reset to the default.'); }
  }
}

function onChange(event, doc) {
  const target = event.target.closest('[data-act]');
  if (!target) return;
  const act = target.dataset.act;
  if (act === 'group-name') {
    const id = target.dataset.group;
    const before = name(getLayout(), id);
    const next = target.value.trim();
    if (!next) { target.value = before; return; }
    change(doc, (layout) => renameGroup(layout, id, next), `Renamed ${before} to ${next}.`);
    return;
  }
  if (act === 'group-icon') {
    const id = target.dataset.group;
    change(doc, (layout) => setGroupIcon(layout, id, target.value), '', { 'data-act': 'group-icon', 'data-group': id });
    return;
  }
  if (act === 'item-move' && target.value) {
    const key = target.dataset.item;
    const to = target.value;
    change(doc, (layout) => moveItem(layout, key, to), `Moved ${itemLabel(key, doc)} to ${name(getLayout(), to)}.`, { 'data-act': 'item-move', 'data-item': key });
  }
}

function onKey(event, doc) {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    if (_state.deleting) {
      const id = _state.deleting;
      _state.deleting = null;
      render(doc, { 'data-act': 'group-delete', 'data-group': id });
    } else closeNavEditor(doc);
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = Array.from(_state.root.querySelectorAll('button, input, select')).filter((node) => !node.disabled);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first.focus(); }
}

export function closeNavEditor(doc = document) {
  if (!_state) return;
  const { root, unsubscribe, opener } = _state;
  _state = null;
  unsubscribe();
  root.remove();
  if (opener?.isConnected) opener.focus?.();
}

export function isNavEditorOpen() { return !!_state; }

/** Open the dialog. `group` puts focus on that group's name; `deleting` also
 *  opens its "where do its tools go" step. */
export function openNavEditor({ doc = document, group = null, deleting = false } = {}) {
  if (_state) closeNavEditor(doc);
  const root = el(doc, 'div', { id: DIALOG_ID, class: 'nav-editor-backdrop' });
  const dialog = el(doc, 'div', { class: 'nav-editor', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'nav-editor-title' }, [
    el(doc, 'div', { class: 'nav-editor-head' }, [
      el(doc, 'h3', { id: 'nav-editor-title', text: 'Edit navigation' }),
      button(doc, '×', 'done', { class: 'nav-editor-close', 'aria-label': 'Close' }),
    ]),
    el(doc, 'p', { class: 'nav-editor-help', text: 'Name groups, put them in order, and move tools between them. Changes save as you make them. You can also drag tools in the sidebar.' }),
    el(doc, 'div', { class: 'nav-editor-body' }),
    el(doc, 'div', { class: 'nav-editor-status', role: 'status', 'aria-live': 'polite' }),
    el(doc, 'div', { class: 'nav-editor-foot' }, [
      button(doc, 'New group', 'new-group'),
      button(doc, 'Reset to default', 'reset'),
      button(doc, 'Done', 'done', { class: 'primary' }),
    ]),
  ]);
  root.appendChild(dialog);
  root.addEventListener('click', (event) => {
    if (event.target === root) closeNavEditor(doc);
    else onClick(event, doc);
  });
  root.addEventListener('change', (event) => onChange(event, doc));
  root.addEventListener('keydown', (event) => onKey(event, doc));
  doc.body.appendChild(root);
  _state = { doc, root, deleting: deleting && group ? group : null, opener: doc.activeElement, unsubscribe: subscribe(() => render(doc)) };
  render(doc, group ? { 'data-act': _state.deleting ? 'delete-confirm' : 'group-name', 'data-group': group } : { 'data-act': 'group-name' });
  return root;
}

export default { openNavEditor, closeNavEditor, isNavEditorOpen };
