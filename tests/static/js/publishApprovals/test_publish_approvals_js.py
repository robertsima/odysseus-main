"""The publish review window shows only text an agent wrote, escaped.

Title, description, file names, sensitive paths and the diff all come from
the agent. Unescaped, a crafted title could run script in the approver's
page and press "Approve and publish" itself. This renders a hostile request
with the real code (static/js/publishApprovals.js) in Node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests import REPO_ROOT

ROOT = REPO_ROOT
MODULE = ROOT / "static" / "js" / "publishApprovals.js"

HARNESS = r"""
const { pathToFileURL } = require('url');
globalThis.window = { addEventListener() {}, location: { origin: 'http://localhost' } };
globalThis.document = { addEventListener() {}, getElementById() { return null; }, visibilityState: 'hidden' };
globalThis.setInterval = () => 0;
const evil = '<img src=x onerror="fetch(1)"><script>alert(1)</script>';
import(pathToFileURL(process.argv[1]).href).then((m) => {
  const t = m._forTests;
  const html = t.reviewHtml({
    id: evil, status: 'pending', can_decide: true, title: evil, body: evil, repo: evil, branch: evil,
    head_sha: evil, base_branch: evil, requested_by: evil, created_at: 1,
    changed_files: [evil], sensitive: { [evil]: [evil] }, blockers: [],
    review: { base: evil, patch: '+' + evil + '\n-' + evil + '\n@@ ' + evil, truncated: false },
  });
  const blocked = t.reviewHtml({ id: 'x', status: 'pending', can_decide: true, title: 't', branch: 'b',
    changed_files: [], sensitive: {}, blockers: [evil], review: { error: evil } });
  console.log(JSON.stringify({
    html, blocked,
    js: t.safeHref('javascript:alert(1)'), data: t.safeHref('data:text/html,x'),
    https: t.safeHref('https://github.com/o/r/pull/1'),
  }));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_agent_written_text_is_escaped_and_links_are_http_only():
    out = subprocess.run(["node", "-e", HARNESS, str(MODULE)], capture_output=True, text=True,
                         timeout=30, check=True)
    res = json.loads(out.stdout)
    for html in (res["html"], res["blocked"]):
        assert "<script" not in html and "<img" not in html and 'onerror="' not in html
        assert "&lt;script&gt;" in html
    assert 'data-id="&lt;img' in res["html"]
    assert res["js"] == "" and res["data"] == ""
    assert res["https"] == "https://github.com/o/r/pull/1"
