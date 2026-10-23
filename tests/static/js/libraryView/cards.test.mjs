// The Library "All" view (static/js/libraryView.js) turns the
// /api/library/items contract into cards. These tests feed it the contract
// shape through the fetch fake and read what a person would see.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const file = (path, type, extra = {}) => ({
  id: `file:${path}`, kind: 'file', title: path.split('/').pop(), type, path,
  updated_at: '2026-10-20T10:00:00Z', ...extra,
});

const GROUP = {
  id: 'group:Projects/Tall Tree Group/Agreements/initial-server-setup',
  kind: 'group', title: 'Tall Tree — Initial Server Setup', type: 'markdown', role: 'master',
  project: 'Tall Tree Group', breadcrumb: ['Projects', 'Tall Tree Group', 'Agreements'],
  path: 'Projects/Tall Tree Group/Agreements/Initial Server Setup.md',
  updated_at: new Date(Date.now() - 2 * 3600e3).toISOString(), locked: false,
  actions: ['open', 'edit', 'download', 'history'],
  group: {
    master: file('Projects/Tall Tree Group/Agreements/Initial Server Setup.md', 'markdown'),
    current_export: file('Projects/Tall Tree Group/Agreements/Initial Server Setup.pdf', 'pdf'),
    signed: [],
    history: [
      file('Projects/Tall Tree Group/Agreements/Backups/Initial Server Setup v2.md', 'markdown', { title: 'Setup v2 newest' }),
      file('Projects/Tall Tree Group/Agreements/Backups/Initial Server Setup v1.md', 'markdown', { title: 'Setup v1 older' }),
    ],
    supporting: [file('Projects/Tall Tree Group/Agreements/Receipts/setup.sha256', 'other', { title: 'setup.sha256' })],
    warnings: ['word_newer_than_master'],
  },
};

const SIGNED = {
  ...GROUP, id: 'group:Projects/Tall Tree Group/Agreements/msa', title: 'Tall Tree MSA',
  locked: true, role: 'signed', actions: ['open', 'download'],
  group: {
    master: null, current_export: null,
    signed: [file('Projects/Tall Tree Group/Agreements/MSA signed.pdf', 'pdf')],
    history: [], supporting: [], warnings: [],
  },
};

const NOTE = {
  id: 'note:n1', kind: 'note', title: 'Call notes', type: 'note', role: null, project: null,
  breadcrumb: [], updated_at: '2026-10-21T09:00:00Z', locked: false, actions: ['open'],
};

const DEFAULT = () => ({ items: [GROUP, SIGNED, NOTE], total: 3, projects: ['Tall Tree Group'], vault_shared: true });
let respond = DEFAULT;
fake.route('GET', '/api/library/items', ({ url }) => respond(url));
fake.route('GET', '/api/library/projects', () => ({
  projects: [{ name: 'Tall Tree Group', path: 'AI Mind/Projects/Tall Tree Group', counts: { documents: 2, files: 9, notes: 1 }, updated_at: '2026-10-22T08:00:00Z' }],
}));

const { createLibraryView } = await import('../../../../static/js/libraryView.js');

function mount(extra = {}) {
  const opened = { vault: [], docs: [], notes: [] };
  const menus = [];
  const view = createLibraryView({
    api: '',
    openVaultFile: (p) => opened.vault.push(p),
    openDocument: (id) => opened.docs.push(id),
    openNote: (id) => opened.notes.push(id),
    showMenu: (anchor, items) => menus.push(items),
    ...extra,
  });
  document.body.appendChild(view.el);
  return { view, opened, menus };
}

const cardFor = (view, id) => [...view.el.querySelectorAll('.lib-card')].find((c) => c.dataset.itemId === id);
const buttons = (card) => [...card.querySelectorAll('.lib-card-actions button')].map((b) => b.textContent.trim());

test('a group card states its roles in words and keeps the path out of the text', async () => {
  const { view } = mount();
  view.activate();
  await waitFor(() => view.el.querySelector('.lib-card'), { what: 'cards' });
  const card = cardFor(view, GROUP.id);
  const text = card.textContent;
  assert.match(text, /Working master: Markdown · Current export: PDF/);
  assert.match(text, /Tall Tree Group › Agreements/);
  assert.ok(!text.includes('Initial Server Setup.md'), 'no filename or path as text');
  assert.ok(!text.includes('Projects/'), 'no path as text');
  assert.equal(card.querySelector('.lib-crumb').getAttribute('title'), GROUP.group.master.path, 'path lives in the tooltip');
  assert.deepEqual(buttons(card).slice(0, 2), ['Edit', 'Open PDF']);
  assert.match(text, /The Word copy is newer than the master\. Open both to compare\./);
  view.el.remove();
});

test('a signed copy shows a lock and has no Edit', async () => {
  const { view } = mount();
  view.activate();
  await waitFor(() => cardFor(view, SIGNED.id), { what: 'signed card' });
  const card = cardFor(view, SIGNED.id);
  assert.ok(card.querySelector('.lib-lock'), 'lock icon');
  assert.match(card.textContent, /Signed copy/);
  assert.ok(!buttons(card).includes('Edit'));
  assert.ok(buttons(card).includes('Open PDF'));
  view.el.remove();
});

test('history stays hidden until opened, newest first, with supporting files apart', async () => {
  const { view } = mount();
  view.activate();
  await waitFor(() => cardFor(view, GROUP.id), { what: 'group card' });
  const card = cardFor(view, GROUP.id);
  const panel = card.querySelector('.lib-history');
  const toggle = card.querySelector('.lib-history-toggle');
  assert.equal(toggle.textContent.trim(), 'History (2)');
  assert.equal(panel.hidden, true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');

  toggle.click();
  assert.equal(panel.hidden, false);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  const lists = panel.querySelectorAll('.lib-history-list');
  assert.equal(lists.length, 2, 'history and supporting files are separate lists');
  const names = [...lists[0].querySelectorAll('.lib-history-name')].map((n) => n.textContent);
  assert.deepEqual(names, ['Setup v2 newest', 'Setup v1 older']);
  assert.ok(lists[1].querySelector('.quiet'), 'supporting files are the quieter list');

  toggle.click();
  assert.equal(panel.hidden, true);
  view.el.remove();
});

test('Edit opens the master in the vault editor; Download and Copy path sit in the overflow', async () => {
  const { view, opened, menus } = mount();
  view.activate();
  await waitFor(() => cardFor(view, GROUP.id), { what: 'group card' });
  const card = cardFor(view, GROUP.id);
  [...card.querySelectorAll('button')].find((b) => b.textContent === 'Edit').click();
  assert.deepEqual(opened.vault, [GROUP.group.master.path]);
  card.querySelector('.lib-overflow').click();
  assert.deepEqual(menus.at(-1).map((m) => m.label), ['Download', 'Copy path']);
  view.el.remove();
});

test('document and note cards open by id', async () => {
  respond = () => ({
    items: [
      { id: 'doc:d-1', kind: 'document', title: 'Notes draft', type: 'document', breadcrumb: [], updated_at: '2026-10-21T09:00:00Z', actions: ['open'] },
      NOTE,
    ],
    total: 2, projects: [], vault_shared: true,
  });
  const { view, opened } = mount();
  view.activate();
  await waitFor(() => cardFor(view, 'doc:d-1'), { what: 'document card' });
  cardFor(view, 'doc:d-1').querySelector('.lib-btn').click();
  cardFor(view, NOTE.id).querySelector('.lib-btn').click();
  assert.deepEqual(opened.docs, ['d-1']);
  assert.deepEqual(opened.notes, ['n1']);
  assert.equal(cardFor(view, 'doc:d-1').querySelector('.lib-history-toggle'), null, 'fewer lines on a plain document');
  view.el.remove();
  respond = DEFAULT;
});

test('a 404 from an older server reports the view unavailable and renders nothing broken', async () => {
  respond = () => new Response('{"detail":"Not Found"}', { status: 404, headers: { 'Content-Type': 'application/json' } });
  let unavailable = 0;
  const { view } = mount({ onUnavailable: () => { unavailable += 1; } });
  view.activate();
  await waitFor(() => unavailable === 1, { what: 'the fallback callback' });
  assert.equal(view.unavailable, true);
  assert.equal(view.el.querySelectorAll('.lib-card').length, 0);
  assert.ok(!/could not be loaded/i.test(view.el.textContent), 'no error banner for an older server');
  view.el.remove();
  respond = DEFAULT;
});

test('filters send kind=, the project chip sends project= and can be removed', async () => {
  respond = () => ({ items: [], total: 0, projects: [], vault_shared: true });
  const { view } = mount();
  const lastQuery = () => fake.calls.filter((c) => c.url.pathname === '/api/library/items').at(-1).url.searchParams;

  view.activate();
  await waitFor(() => fake.calls.some((c) => c.url.pathname === '/api/library/items'), { what: 'first request' });
  assert.equal(lastQuery().get('kind'), 'all');

  const b1 = fake.calls.length;
  view.setKind('note'); view.activate();
  await waitFor(() => fake.calls.length > b1, { what: 'notes request' });
  assert.equal(lastQuery().get('kind'), 'note');

  const b2 = fake.calls.length;
  view.setKind('file'); view.activate();
  await waitFor(() => fake.calls.length > b2, { what: 'files request' });
  assert.equal(lastQuery().get('kind'), 'file');

  // Projects: one card per project, clicking one filters to it.
  view.setKind('project'); view.activate();
  await waitFor(() => view.el.querySelector('.lib-project-card'), { what: 'project card' });
  const pc = view.el.querySelector('.lib-project-card');
  assert.match(pc.textContent, /Tall Tree Group/);
  assert.match(pc.textContent, /2 documents · 9 files · 1 note/);
  const b3 = fake.calls.length;
  pc.click();
  await waitFor(() => view.el.querySelector('.lib-project-chip'), { what: 'project chip' });
  assert.equal(view.el.querySelector('.lib-project-chip').textContent.replace('×', '').trim(), 'Project: Tall Tree Group');
  await waitFor(() => fake.calls.length > b3, { what: 'project request' });
  assert.equal(lastQuery().get('project'), 'Tall Tree Group');
  assert.equal(lastQuery().get('kind'), 'all');

  const b4 = fake.calls.length;
  view.el.querySelector('.lib-chip-x').click();
  await waitFor(() => fake.calls.length > b4, { what: 'unfiltered request' });
  assert.equal(lastQuery().has('project'), false);
  await waitFor(() => !view.el.querySelector('.lib-project-chip'), { what: 'the chip to go' });
  view.el.remove();
  respond = DEFAULT;
});
