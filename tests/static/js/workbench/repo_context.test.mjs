// Changes / Commits loading (static/js/workbench.js): which repository a
// response belongs to, what a refresh keeps, and what the commit count says.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake, jsonResponse } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

globalThis.EventSource = class EventSource {
  addEventListener() {}
  removeEventListener() {}
  close() {}
};
window.sessionModule = { getCurrentSessionId: () => 's1' };
fake.route('GET', '/api/auth/settings', () => ({ workbench_enabled: true }));

const workbench = (await import('../../../../static/js/workbench.js')).default;
const { _state: state, _repo: repo } = await import('../../../../static/js/workbench.js');
await workbench.init();

const A = '/srv/a';
const B = '/srv/b';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const file = (path) => ({ path, status: 'modified', additions: 1, deletions: 0 });
const commit = (n) => ({ sha: String(n).padStart(40, '0'), short: String(n), author: 't', date: '2026-10-01T00:00:00Z', subject: `commit ${n}` });
const page = (from, count, extra = {}) => ({
  commits: Array.from({ length: count }, (_, i) => commit(from - i)),
  has_more: false, total: from, next_offset: null, scope: 'repository', ...extra,
});

// What each repository answers; a test replaces an entry with a deferred.
let answers;
let changesCalls;
let taskCalls;
function reset() {
  answers = {
    [A]: { changes: () => ({ files: [file('a.txt')] }), commits: () => page(3, 3) },
    [B]: { changes: () => ({ files: [file('b.txt')] }), commits: () => page(2, 2) },
  };
  changesCalls = [];
  taskCalls = 0;
  fake.calls.length = 0;
}
fake.route('GET', '/api/workbench/repo/changes', ({ url }) => {
  const path = url.searchParams.get('path');
  changesCalls.push(path);
  return answers[path].changes();
});
fake.route('GET', '/api/workbench/repo/commits', ({ url }) => answers[url.searchParams.get('path')].commits(url));
fake.route('GET', '/api/workbench/repo/diff', ({ url }) => ({ diff: `diff of ${url.searchParams.get('file')}` }));
fake.route('GET', '/api/claude-code/tasks/t1/changes', () => { taskCalls += 1; return answers.task(); });
fake.route('GET', '/api/claude-code/tasks/t1/commits', ({ url }) => answers.taskMore(url));

beforeEach(() => {
  document.body.innerHTML = '<div id="workbench-modal"><div id="wb-changes"></div><div id="wb-commits"></div></div>';
  state.prefs.tab = 'changes';
  state.editor = null;
  state.repos = [];
  reset();
});

const changesText = () => document.getElementById('wb-changes').textContent;
const commitsText = () => document.getElementById('wb-commits').textContent;

test('a delayed answer for A does not replace B, or end B\'s loading state', async () => {
  const slowA = deferred();
  const slowB = deferred();
  answers[A].changes = () => slowA.promise;
  answers[B].changes = () => slowB.promise;

  repo.setRepo(A, { remember: false });
  await tick();
  repo.setRepo(B, { remember: false });
  await tick();
  slowA.resolve({ files: [file('a.txt')] });
  await tick(); await tick();

  assert.deepEqual(state.files, [], 'A\'s files leaked into B');
  assert.equal(state.filesLoading, true, 'A finishing ended B\'s loading indicator');
  assert.match(changesText(), /Loading changes/);

  slowB.resolve({ files: [file('b.txt')] });
  await tick(); await tick();
  assert.deepEqual(state.files.map((f) => f.path), ['b.txt']);
  assert.equal(state.filesLoading, false);
});

test('a delayed failure for A does not mark B as failed or stop its loading', async () => {
  const slowA = deferred();
  const slowB = deferred();
  answers[A].changes = () => slowA.promise;
  answers[B].changes = () => slowB.promise;

  repo.setRepo(A, { remember: false });
  await tick();
  repo.setRepo(B, { remember: false });
  await tick();
  slowA.resolve(jsonResponse({ detail: 'git exploded' }, { status: 500 }));
  await tick(); await tick();

  assert.equal(state.repoStatus.changes.error, '');
  assert.equal(state.filesLoading, true);
  assert.doesNotMatch(changesText(), /git exploded/);

  slowB.resolve({ files: [file('b.txt')] });
  await tick(); await tick();
  assert.deepEqual(state.files.map((f) => f.path), ['b.txt']);
  assert.equal(state.repoStatus.changes.error, '');
});

test('a diff or commit that arrives after switching repositories is dropped', async () => {
  repo.setRepo(A, { remember: false });
  await tick(); await tick();
  const slowDiff = deferred();
  fake.route('GET', '/api/workbench/repo/commit', () => slowDiff.promise);
  const pending = repo.selectCommit(commit(3).sha);
  repo.setRepo(B, { remember: false });
  await tick(); await tick();
  slowDiff.resolve({ sha: commit(3).sha, subject: 'from A', files: [] });
  await pending;
  assert.equal(state.selectedCommit, null, 'A\'s commit became the selection in B');
});

test('Refresh on a run\'s Commits tab fetches the run again and shows new commits', async () => {
  const task = (commits) => ({
    repository: A, start_commit: 'abc1234567', label: 'fix it', status: 'running', branch: 'agent/x',
    changes: [file('a.txt')], commits,
    commits_page: { has_more: false, total: commits.length, next_offset: null, scope: 'since_base', limit: 50 },
  });
  answers.task = () => task([commit(1)]);
  state.prefs.tab = 'commits';
  await repo.loadTask('t1', { tab: 'commits' });
  assert.equal(state.commits.length, 1);

  answers.task = () => task([commit(2), commit(1)]);
  await repo.refreshRepo('manual');

  assert.equal(taskCalls, 2);
  assert.deepEqual(state.commits.map((k) => k.subject), ['commit 2', 'commit 1']);
  assert.match(commitsText(), /commit 2/);
  assert.match(commitsText(), /since the run started/);
});

test('a capped list says "Latest 50" or "50 of 312" and Load more adds the next page', async () => {
  answers[A].commits = (url) => {
    if (url.searchParams.get('offset')) {
      assert.equal(url.searchParams.get('total'), 'false');
      return page(312 - 50, 50, { has_more: true, total: null, next_offset: 100 });
    }
    return page(312, 50, { has_more: true, total: 312, next_offset: 50 });
  };
  state.prefs.tab = 'commits';
  await repo.setRepo(A, { remember: false });

  assert.match(commitsText(), /50 of 312/);
  assert.match(commitsText(), /in the repository log/);
  assert.doesNotMatch(commitsText(), /50 commits/);
  assert.match(commitsText(), /Load more/);

  await repo.loadMoreCommits();
  assert.equal(state.commits.length, 100);
  assert.match(commitsText(), /100 of 312/);

  // Without a total the label cannot claim one.
  answers[A].commits = () => page(312, 50, { has_more: true, total: null, next_offset: 50 });
  await repo.refreshRepo('manual');
  assert.match(commitsText(), /Latest 50/);
  assert.doesNotMatch(commitsText(), /of \d+/);
});

test('a complete history is not labelled as capped', async () => {
  answers[A].commits = () => page(12, 12);
  state.prefs.tab = 'commits';
  await repo.setRepo(A, { remember: false });
  assert.match(commitsText(), /12 commits/);
  assert.doesNotMatch(commitsText(), /Latest|Load more/);
});

test('a failed refresh keeps the previous data, marked stale, with Retry', async () => {
  await repo.setRepo(A, { remember: false });
  assert.deepEqual(state.files.map((f) => f.path), ['a.txt']);

  answers[A].changes = () => jsonResponse({ detail: 'git timed out' }, { status: 500 });
  await repo.refreshRepo('manual');

  assert.deepEqual(state.files.map((f) => f.path), ['a.txt']);
  assert.match(changesText(), /a\.txt/);
  assert.match(changesText(), /git timed out/);
  assert.match(changesText(), /Stale/);
  assert.ok(document.querySelector('#wb-changes .wb-inline-error [data-wb-act="refresh-repo"]'));
  assert.doesNotMatch(changesText(), /Working tree is clean/);

  answers[A].changes = () => ({ files: [file('a.txt'), file('c.txt')] });
  await repo.refreshRepo('manual');
  assert.equal(state.files.length, 2);
  assert.equal(state.repoStatus.changes.error, '');
});

test('a first load that fails says so instead of showing a clean tree', async () => {
  answers[A].changes = () => jsonResponse({ detail: 'not a git checkout' }, { status: 400 });
  await repo.setRepo(A, { remember: false });
  assert.match(changesText(), /not a git checkout/);
  assert.doesNotMatch(changesText(), /Working tree is clean/);
});

test('a refresh keeps the selected file and the unsaved editor draft', async () => {
  answers[A].changes = () => ({ files: [file('a.txt'), file('b.txt')] });
  await repo.setRepo(A, { remember: false });
  await repo.selectFile('b.txt');
  state.editor = { path: 'b.txt', version: 'v', content: 'old', dirty: true };
  document.getElementById('wb-diffpane').innerHTML = '<textarea id="wb-editor-text"></textarea>';
  document.getElementById('wb-editor-text').value = 'my draft';

  // b.txt left the change list (an agent committed it) while the draft is open.
  answers[A].changes = () => ({ files: [file('a.txt')] });
  await repo.refreshRepo('manual');

  assert.equal(state.selectedFile, 'b.txt');
  assert.equal(state.editor?.dirty, true);
  assert.equal(document.getElementById('wb-editor-text')?.value, 'my draft');
});

test('simultaneous refreshes share one pass and one trailing pass', async () => {
  await repo.setRepo(A, { remember: false });
  changesCalls.length = 0;
  const slow = deferred();
  answers[A].changes = () => slow.promise;

  const runs = [repo.refreshRepo('manual'), repo.refreshRepo('visible'), repo.refreshRepo('stale'), repo.refreshRepo('manual')];
  await tick();
  assert.equal(changesCalls.length, 1);
  answers[A].changes = () => ({ files: [file("z.txt")] });
  slow.resolve({ files: [file('a.txt')] });
  await Promise.all(runs);

  assert.equal(changesCalls.length, 2, 'four triggers should cost one pass plus one trailing pass');
  assert.deepEqual(state.files.map((f) => f.path), ['z.txt']);
});

test('wb:repo-stale from Activity refreshes the open Changes tab', async () => {
  await repo.setRepo(A, { remember: false });
  changesCalls.length = 0;
  answers[A].changes = () => ({ files: [file("a.txt"), file("new.txt")] });

  document.dispatchEvent(new CustomEvent('wb:repo-stale', { detail: { session_id: 's1', run_id: 'r', reason: 'run_finished' } }));
  document.dispatchEvent(new CustomEvent('wb:repo-stale', { detail: { session_id: 's1', run_id: 'r', reason: 'tool_result' } }));
  await new Promise((r) => setTimeout(r, 400));

  assert.equal(changesCalls.length, 1, 'two events in a burst make one refresh');
  assert.equal(state.files.length, 2);
});

test('the scope header names repository, branch, base and last-updated time', async () => {
  state.repos = [{ path: A, branch: 'feature/x', kind: 'checkout' }];
  answers[A].changes = () => ({ files: [file('a.txt')] });
  await repo.setRepo(A, { remember: false });
  const header = document.querySelector('#wb-changes .wb-scope');
  assert.ok(header);
  assert.match(header.textContent, /Repository a/);
  assert.match(header.textContent, /Branch feature\/x/);
  assert.match(header.textContent, /Compared with HEAD/);
  assert.match(header.textContent, /Last updated \d\d:\d\d:\d\d/);
  assert.ok(header.querySelector('[data-wb-act="refresh-repo"]'));
  assert.ok(header.querySelector('details'));
});
