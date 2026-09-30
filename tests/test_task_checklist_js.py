"""The checklist above the composer (static/js/taskChecklist.js), run in Node.

The checklist is written by the agent, so its text is escaped before it
reaches the page; the parser reads the same markdown as src/task_checklist.py
(`- [ ]` open, `- [x]` done, `[~]`/`[-]` dropped and counted as done).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src import task_checklist

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "static" / "js" / "taskChecklist.js"

PLAN = (
    "Intro line that is not an item\n"
    "- [x] read the failing test\n"
    "- [ ] fix <img src=x onerror=\"fetch(1)\"> the handler\n"
    "* [~] dropped step\n"
    "- [ ] run the suite\n"
)

HARNESS = r"""
const { pathToFileURL } = require('url');
globalThis.window = { addEventListener() {} };
globalThis.document = { addEventListener() {}, getElementById() { return null; } };
import(pathToFileURL(process.argv[1]).href).then((m) => {
  const t = m._forTests;
  const items = t.parsePlan(process.argv[2]);
  console.log(JSON.stringify({ items, open: t.checklistHtml(items, false), shut: t.checklistHtml(items, true) }));
});
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_panel_parses_like_the_server_and_escapes_agent_text():
    out = subprocess.run(["node", "-e", HARNESS, str(MODULE), PLAN], capture_output=True,
                         text=True, timeout=30, check=True)
    res = json.loads(out.stdout)
    assert res["items"] == task_checklist.items(PLAN)
    html = res["open"]
    assert "<img" not in html and "&lt;img" in html
    assert "2/4" in html  # done and dropped both count as done
    assert 'class="tc-item tc-current"' in html  # the first open step is marked current
    assert html.count('class="tc-item tc-current"') == 1
    assert 'aria-expanded="true"' in html
    assert 'aria-expanded="false"' in res["shut"] and "<ol id=\"tc-list\" class=\"tc-list\" hidden>" in res["shut"]
