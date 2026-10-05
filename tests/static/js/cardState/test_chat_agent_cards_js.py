"""Chat UI: collapsed sub-agent cards and the per-turn activity summary.

* Worker cards (the live run card and a worker's handed-back result) remember
  being opened across a refresh — ``cardState.js``, driven through node so the
  real module runs against a stub localStorage.
* A turn's tool calls are summarised in one line ("Searched 3 times, read 2
  files, ran 1 worker") — ``agentThread.activitySummary``, also run in node.

How the cards, the timeline and the Control Room's Conversation tab look and
behave in the page is covered in tests/static/js/chatRenderer/test_agent_cards.py
and tests/static/js/agentsDashboard/test_control_room.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js"
CARD_STATE = (JS / "cardState.js").read_text(encoding="utf-8")
AGENT_THREAD = (JS / "agentThread.js").read_text(encoding="utf-8")

_HAS_NODE = shutil.which("node") is not None
needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node is not installed")


def _run(src: str) -> object:
    out = subprocess.run(["node", "--input-type=module", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_LOCAL_STORAGE = r"""
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
};
"""


@needs_node
def test_cards_are_collapsed_unless_opened_and_the_choice_survives_a_reload():
    src = _LOCAL_STORAGE + CARD_STATE.replace("export default", "const _default =") + r"""
const before = isCardOpen('run:r1');
setCardOpen('run:r1', true);
const opened = isCardOpen('run:r1');
const other = isCardOpen('run:r2');
setCardOpen('run:r1', false);
const closed = isCardOpen('run:r1');
store.set('odysseus-agent-cards-open', 'not json');
const corrupt = isCardOpen('run:r1');
for (let i = 0; i < 400; i++) setCardOpen('run:' + i, true);
const kept = JSON.parse(store.get('odysseus-agent-cards-open')).length;
console.log(JSON.stringify({ before, opened, other, closed, corrupt, kept, newest: isCardOpen('run:399'), oldest: isCardOpen('run:0') }));
"""
    r = _run(src)
    assert r["before"] is False, "nothing stored means collapsed"
    assert r["opened"] is True and r["other"] is False
    assert r["closed"] is False
    assert r["corrupt"] is False, "a bad stored value falls back to collapsed"
    assert r["kept"] == 300 and r["newest"] is True and r["oldest"] is False


@needs_node
def test_a_turns_tool_calls_read_as_one_line_of_activity():
    src = "globalThis.window = {};\n" + AGENT_THREAD.replace("export default", "const _default =") + r"""
console.log(JSON.stringify([
  activitySummary(['web_search', 'web_search', 'grep', 'read_file', 'read_document', 'manage_agent_loadout']),
  activitySummary(['bash', 'write_file', 'edit_file', 'bash', 'bash']),
  activitySummary(['web_search']),
  activitySummary(['ui_control', 'manage_memory']),
  activitySummary(['read_file', 'ui_control']),
  activitySummary([]),
]));
"""
    r = _run(src)
    assert r[0] == "Searched 3 times, read 2 files, ran 1 worker"
    assert r[1] == "Ran 3 commands, edited 2 files"
    assert r[2] == "Searched once"
    assert r[3] == "Used 2 tools"
    assert r[4] == "Read 1 file, used 1 other tool"
    assert r[5] == ""
