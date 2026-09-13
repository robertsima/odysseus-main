/* diffView.js — one diff renderer for the whole UI.
 *
 * The chat's tool cards (live in chat.js, replayed in chatRenderer.js) and
 * the Workbench's Changes / Commits / Pull Request views all show unified
 * diffs. They used to carry three copies of the same span-per-line loop;
 * this module owns it, plus a parser that turns unified-diff text into
 * files → hunks → numbered lines so the Workbench can draw a side-by-side
 * ("old vs new") view and let a reviewer point at a line.
 *
 * Pure functions, no DOM access at import time: Node tests import it directly.
 */

// Local escaper: ui.js pulls in the whole widget layer at import time, which
// would make this module untestable outside a browser.
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _emptyFile() {
  return { header: '', oldPath: '', newPath: '', hunks: [], binary: false, meta: [] };
}

function _stripPathPrefix(p) {
  p = String(p || '').trim();
  // "a/x.py" / "b/x.py" from git, "/dev/null" for added/deleted files,
  // and an optional trailing tab-separated timestamp from difflib.
  p = p.split('\t')[0];
  if (p === '/dev/null') return '';
  return p.replace(/^[ab]\//, '');
}

/**
 * Parse unified-diff text into
 *   [{ header, oldPath, newPath, binary, hunks: [{ header, oldStart, newStart,
 *      context, lines: [{ type: 'ctx'|'add'|'del'|'meta', text, oldNo, newNo }] }] }]
 * `---`/`+++` are file headers only outside a hunk (a deleted line whose own
 * text begins with "-- " is otherwise indistinguishable), and a hunk ends
 * when the counts in its `@@` header are used up.
 */
export function parseUnifiedDiff(text) {
  const files = [];
  let cur = null;
  let hunk = null;
  let oldLeft = 0;
  let newLeft = 0;
  let oldNo = 0;
  let newNo = 0;
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  const ensureFile = () => {
    if (!cur) { cur = _emptyFile(); files.push(cur); }
    return cur;
  };
  const inHunk = () => hunk && (oldLeft > 0 || newLeft > 0);

  for (const raw of lines) {
    if (!inHunk()) {
      if (raw.startsWith('diff --git ') || raw.startsWith('diff --cc ')) {
        cur = _emptyFile();
        cur.header = raw;
        files.push(cur);
        hunk = null;
        const m = /^diff --git a\/(.*?) b\/(.*)$/.exec(raw);
        if (m) { cur.oldPath = m[1]; cur.newPath = m[2]; }
        continue;
      }
      if (raw.startsWith('--- ')) {
        const f = ensureFile();
        if (hunk && f.hunks.length) {
          // A second `---` after a finished hunk with no `diff --git`
          // (difflib output for several files): start a new file record.
          cur = _emptyFile(); files.push(cur); hunk = null;
        }
        ensureFile().oldPath = _stripPathPrefix(raw.slice(4)) || ensureFile().oldPath;
        continue;
      }
      if (raw.startsWith('+++ ')) {
        ensureFile().newPath = _stripPathPrefix(raw.slice(4)) || ensureFile().newPath;
        continue;
      }
      if (raw.startsWith('Binary files')) {
        ensureFile().binary = true;
        continue;
      }
      if (raw.startsWith('@@')) {
        const m = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/.exec(raw);
        const f = ensureFile();
        hunk = {
          header: raw,
          oldStart: m ? Number(m[1]) : 0,
          newStart: m ? Number(m[3]) : 0,
          context: m ? m[5] : '',
          lines: [],
        };
        oldLeft = m ? (m[2] === undefined ? 1 : Number(m[2])) : 0;
        newLeft = m ? (m[4] === undefined ? 1 : Number(m[4])) : 0;
        oldNo = hunk.oldStart;
        newNo = hunk.newStart;
        f.hunks.push(hunk);
        continue;
      }
      if (raw === '' && !cur) continue;
      if (cur && !hunk) cur.meta.push(raw);
      continue;
    }
    // Inside a hunk.
    if (raw.startsWith('\\')) { hunk.lines.push({ type: 'meta', text: raw }); continue; }
    if (raw.startsWith('+')) { hunk.lines.push({ type: 'add', text: raw.slice(1), newNo: newNo++ }); newLeft--; continue; }
    if (raw.startsWith('-')) { hunk.lines.push({ type: 'del', text: raw.slice(1), oldNo: oldNo++ }); oldLeft--; continue; }
    hunk.lines.push({ type: 'ctx', text: raw.startsWith(' ') ? raw.slice(1) : raw, oldNo: oldNo++, newNo: newNo++ });
    oldLeft--; newLeft--;
  }
  return files;
}

/** Legacy chat-card body: one span per line, markers stripped, colour encodes add/del. */
export function renderUnifiedRows(text) {
  return String(text || '').split('\n').map((line) => {
    let cls = 'diff-ctx';
    let t = line;
    if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-meta';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    else if (line.startsWith('+')) { cls = 'diff-add'; t = line.slice(1); }
    else if (line.startsWith('-')) { cls = 'diff-del'; t = line.slice(1); }
    else if (line.startsWith(' ')) t = line.slice(1);
    return `<span class="${cls}">${esc(t) || '&nbsp;'}</span>`;
  }).join('');  // spans are display:block — a literal \n would double-space
}

/** The collapsible diff card used inside agent tool nodes (chat + history). */
export function renderDiffCard(d) {
  if (!d || !d.text) return '';
  const stat = [
    d.new_file ? '<span class="diff-stat-new">new</span>' : '',
    d.added ? `<span class="diff-stat-add">+${Number(d.added)}</span>` : '',
    d.removed ? `<span class="diff-stat-del">−${Number(d.removed)}</span>` : '',
  ].filter(Boolean).join(' ');
  return `<details class="agent-tool-output agent-tool-diff"><summary><span class="diff-file">${esc(d.file || 'diff')}</span> <span class="diff-summary-stats">${stat}</span></summary><pre class="diff-pre">${renderUnifiedRows(d.text)}</pre></details>`;
}

function _cell(no) {
  return `<td class="wb-no">${no == null ? '' : no}</td>`;
}

function _code(text) {
  return `<td class="wb-code">${esc(text) || '&nbsp;'}</td>`;
}

/**
 * Numbered diff table for one parsed file.
 *   mode 'unified': old# | new# | code
 *   mode 'split'  : old# | old code | new# | new code (deletions paired with additions)
 * Rows carry data-old / data-new so a reviewer can pick a line to comment on.
 */
export function renderFileTable(file, { mode = 'split', path = '' } = {}) {
  if (!file) return '';
  const p = esc(path || file.newPath || file.oldPath || '');
  if (file.binary) return `<div class="wb-diff-note">Binary file — no text diff.</div>`;
  if (!file.hunks.length) return `<div class="wb-diff-note">No textual changes.</div>`;
  const rows = [];
  for (const h of file.hunks) {
    // Not `wb-no`: the hunk banner must not inherit the narrow, right-aligned,
    // clickable line-number cell styling.
    rows.push(`<tr class="wb-l wb-hunk"><td class="wb-hunk-cell" colspan="${mode === 'split' ? 4 : 3}">${esc(h.header)}</td></tr>`);
    if (mode === 'unified') {
      for (const l of h.lines) {
        // Context rows are `wb-same`: `wb-ctx` is the toolbar's repository label.
        rows.push(`<tr class="wb-l wb-${l.type === 'ctx' ? 'same' : l.type}" data-path="${p}" data-old="${l.oldNo ?? ''}" data-new="${l.newNo ?? ''}">${_cell(l.oldNo)}${_cell(l.newNo)}${_code((l.type === 'add' ? '+' : l.type === 'del' ? '-' : ' ') + l.text)}</tr>`);
      }
      continue;
    }
    // Split: walk the hunk; a run of deletions followed by a run of additions
    // is shown side by side, the rest mirrored.
    let i = 0;
    const L = h.lines;
    while (i < L.length) {
      const l = L[i];
      if (l.type === 'ctx' || l.type === 'meta') {
        rows.push(`<tr class="wb-l wb-same" data-path="${p}" data-old="${l.oldNo ?? ''}" data-new="${l.newNo ?? ''}">${_cell(l.oldNo)}${_code(l.text)}${_cell(l.newNo)}${_code(l.text)}</tr>`);
        i++;
        continue;
      }
      const dels = [];
      const adds = [];
      while (i < L.length && L[i].type === 'del') dels.push(L[i++]);
      while (i < L.length && L[i].type === 'add') adds.push(L[i++]);
      const n = Math.max(dels.length, adds.length);
      for (let k = 0; k < n; k++) {
        const d = dels[k];
        const a = adds[k];
        const cls = d && a ? 'wb-change' : d ? 'wb-del' : 'wb-add';
        rows.push(`<tr class="wb-l ${cls}" data-path="${p}" data-old="${d ? d.oldNo : ''}" data-new="${a ? a.newNo : ''}">`
          // `wb-blank`, not `wb-empty`: that name is the Workbench's padded
          // empty-state block, and it inflated these filler cells.
          + (d ? `${_cell(d.oldNo)}<td class="wb-code wb-side-del">${esc(d.text) || '&nbsp;'}</td>` : '<td class="wb-no"></td><td class="wb-code wb-blank"></td>')
          + (a ? `${_cell(a.newNo)}<td class="wb-code wb-side-add">${esc(a.text) || '&nbsp;'}</td>` : '<td class="wb-no"></td><td class="wb-code wb-blank"></td>')
          + '</tr>');
      }
    }
  }
  return `<table class="wb-diff wb-diff-${mode}" data-path="${p}"><tbody>${rows.join('')}</tbody></table>`;
}

/** Render every file in a unified-diff text. */
export function renderDiffText(text, { mode = 'split', path = '' } = {}) {
  const files = parseUnifiedDiff(text);
  if (!files.length) return `<div class="wb-diff-note">${esc(text ? 'Nothing to show for this file.' : 'No diff.')}</div>`;
  return files.map((f) => {
    const name = path || f.newPath || f.oldPath || '';
    const head = files.length > 1 || !path ? `<div class="wb-diff-filehead">${esc(name)}</div>` : '';
    return head + renderFileTable(f, { mode, path: name });
  }).join('');
}

/** Totals for a unified-diff text: { additions, deletions }. */
export function diffStats(text) {
  let added = 0;
  let removed = 0;
  for (const f of parseUnifiedDiff(text)) {
    for (const h of f.hunks) {
      for (const l of h.lines) {
        if (l.type === 'add') added++;
        else if (l.type === 'del') removed++;
      }
    }
  }
  return { additions: added, deletions: removed };
}

const diffView = { parseUnifiedDiff, renderUnifiedRows, renderDiffCard, renderFileTable, renderDiffText, diffStats };
export default diffView;
