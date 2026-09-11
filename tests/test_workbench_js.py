"""Frontend Workbench: the shared diff renderer (static/js/diffView.js) drives
the chat's diff cards and the Workbench's split / unified tables, so its
parser and renderers are pinned here. Runs under `node --input-type=module`
with the browser globals ui.js touches stubbed; skips when node is absent.
workbench.js itself is DOM-bound, so it only gets a syntax check.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")

_PRELUDE = """
    const noop = () => {};
    const fakeEl = () => new Proxy({}, { get: (t, k) => (k === 'classList' ? { add: noop, remove: noop, toggle: noop, contains: () => false } : (k === 'style' ? {} : (typeof k === 'string' && k.startsWith('add') ? noop : (k === 'querySelector' || k === 'getElementById') ? (() => null) : (k === 'querySelectorAll' ? (() => []) : undefined)))) });
    globalThis.window = globalThis;
    globalThis.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], addEventListener: noop, createElement: fakeEl, body: fakeEl(), documentElement: fakeEl(), readyState: 'complete' };
    globalThis.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
    try { Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node' }, configurable: true }); } catch (_) {}
    globalThis.matchMedia = () => ({ matches: false, addEventListener: noop, addListener: noop });
    globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
    globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
    globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    globalThis.ResizeObserver = class { observe() {} disconnect() {} };
    globalThis.addEventListener = noop;
    globalThis.location = { search: '', hash: '', href: 'http://x/' };
"""

_DIFF = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,4 +1,5 @@
 import os
-x = 1
+x = 2
+y = 3
 print(x)
-- a stray line starting with dashes
+z = 4
"""


def _run(script: str) -> dict:
    res = subprocess.run(["node", "--input-type=module", "-e", _PRELUDE + textwrap.dedent(script)],
                         cwd=_REPO, capture_output=True, timeout=30, text=True)
    if res.returncode != 0:
        raise AssertionError(f"node failed:\n{res.stderr}")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert lines, "no stdout"
    return json.loads(lines[-1])


def test_parse_unified_diff_tracks_line_numbers_and_hunk_bounds():
    """A deleted line whose text starts with "-- " must stay a deletion inside
    the hunk (the old renderer mislabelled it as a file header), and old/new
    numbering must follow the @@ counts."""
    out = _run(f"""
        const {{ parseUnifiedDiff }} = await import('./static/js/diffView.js');
        const files = parseUnifiedDiff({json.dumps(_DIFF)});
        const h = files[0].hunks[0];
        console.log(JSON.stringify({{
          n: files.length, path: files[0].newPath, hunks: files[0].hunks.length,
          types: h.lines.map(l => l.type), old: h.lines.map(l => l.oldNo), nw: h.lines.map(l => l.newNo),
          dashText: h.lines[5].text,
        }}));
    """)
    assert out["n"] == 1 and out["path"] == "src/a.py" and out["hunks"] == 1
    assert out["types"] == ["ctx", "del", "add", "add", "ctx", "del", "add"]
    assert out["old"] == [1, 2, None, None, 3, 4, None]
    assert out["nw"] == [1, None, 2, 3, 4, None, 5]
    assert out["dashText"] == "- a stray line starting with dashes"


def test_diff_card_matches_legacy_markup_and_escapes():
    out = _run("""
        const { renderDiffCard, diffStats } = await import('./static/js/diffView.js');
        const html = renderDiffCard({ file: 'x<y>.py', text: '--- a\\n+++ b\\n@@ -1 +1 @@\\n-<b>old</b>\\n+new & improved', added: 1, removed: 1, new_file: false });
        console.log(JSON.stringify({ html, stats: diffStats('--- a\\n+++ b\\n@@ -1,3 +1,2 @@\\n+a\\n-b\\n-c\\n c') }));
    """)
    html = out["html"]
    assert html.startswith('<details class="agent-tool-output agent-tool-diff"><summary><span class="diff-file">x&lt;y&gt;.py</span>')
    assert '<span class="diff-del">&lt;b&gt;old&lt;/b&gt;</span><span class="diff-add">new &amp; improved</span>' in html
    assert '<span class="diff-stat-add">+1</span>' in html and "\n" not in html.split('<pre class="diff-pre">')[1]
    assert out["stats"] == {"additions": 1, "deletions": 2}


def test_split_and_unified_tables_pair_changes_and_carry_line_data():
    out = _run(f"""
        const {{ renderDiffText }} = await import('./static/js/diffView.js');
        const split = renderDiffText({json.dumps(_DIFF)}, {{ mode: 'split', path: 'src/a.py' }});
        const unified = renderDiffText({json.dumps(_DIFF)}, {{ mode: 'unified', path: 'src/a.py' }});
        const count = (s, re) => (s.match(re) || []).length;
        console.log(JSON.stringify({{
          splitTable: split.includes('class="wb-diff wb-diff-split"'),
          changeRows: count(split, /class="wb-l wb-change"/g),
          sideDel: count(split, /wb-side-del/g), sideAdd: count(split, /wb-side-add/g),
          hasData: split.includes('data-path="src/a.py" data-old="2" data-new="2"'),
          unifiedAdd: count(unified, /class="wb-l wb-add"/g), unifiedDel: count(unified, /class="wb-l wb-del"/g),
          hunk: unified.includes('wb-hunk'),
        }}));
    """)
    assert out["splitTable"] and out["hunk"]
    # 2 deletions paired with 3 additions → 2 change rows plus one unpaired add row
    assert out["changeRows"] == 2 and out["sideDel"] == 2 and out["sideAdd"] == 3
    assert out["hasData"]
    assert out["unifiedAdd"] == 3 and out["unifiedDel"] == 2


def test_workbench_module_parses():
    for name in ("workbench.js", "diffView.js"):
        res = subprocess.run(["node", "--check", str(_REPO / "static" / "js" / name)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


def test_workbench_wired_into_page():
    html = (_REPO / "static" / "index.html").read_text()
    assert 'id="workbench-modal"' in html and 'id="rail-workbench"' in html and 'id="set-workbenchCard"' in html
    app = (_REPO / "static" / "app.js").read_text()
    assert "import workbenchModule from './js/workbench.js'" in app and "'rail-workbench': 'Workbench'" in app
    for name in ("chat.js", "chatRenderer.js"):
        src = (_REPO / "static" / "js" / name).read_text()
        assert "renderDiffCard(" in src
        assert "line.startsWith('+++') || line.startsWith('---')" not in src, f"{name} still carries its own diff loop"
