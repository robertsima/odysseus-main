"""Chat UI: collapsed sub-agent cards, per-turn activity summary, and the
Control Room's sub-agent Conversation tab.

* Worker cards (the live run card and a worker's handed-back result) render
  collapsed and remember being opened across a refresh — ``cardState.js``,
  driven through node so the real module runs against a stub localStorage.
* A turn's tool calls are summarised in one line ("Searched 3 times, read 2
  files, ran 1 worker") — ``agentThread.activitySummary``, also run in node.
* The remaining wiring is pinned by source scans, the idiom
  test_agents_dashboard_static.py already uses for these files.
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
WORKBENCH = (JS / "workbench.js").read_text(encoding="utf-8")
RENDERER = (JS / "chatRenderer.js").read_text(encoding="utf-8")
AGENTS = (JS / "agentsDashboard.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

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


def test_the_timeline_starts_collapsed_behind_the_summary():
    refresh = AGENT_THREAD.split("export function refreshThread", 1)[1].split("export function refreshAllThreads", 1)[0]
    assert "thread.dataset.activity !== 'open'" in refresh
    assert 'data-ats="toggle"' in refresh and "aria-expanded" in refresh
    # A live turn still shows the call it is making right now.
    assert "!n.classList.contains('running')" in refresh
    assert "if (action === 'toggle')" in AGENT_THREAD
    # Both renderers give the classifier the real tool name, not the live label.
    assert "node.dataset.tool = ev.tool" in RENDERER
    assert "node.dataset.tool = json.tool" in (JS / "chat.js").read_text(encoding="utf-8")


def test_worker_run_cards_are_collapsed_by_default_even_while_running():
    card = WORKBENCH.split("function updateChatCard(ev)", 1)[1].split("// ── window plumbing", 1)[0]
    assert "isCardOpen(`run:${ev.run_id}`)" in card
    assert "setCardOpen(`run:${ev.run_id}`, opened)" in card
    # Running used to force the card open in CSS.
    assert ".agent-run-card.running .agent-run-steps" not in STYLE
    assert ".agent-run-card:not(.open) .agent-run-foot { display: none; }" in STYLE


def test_a_workers_handed_back_result_is_one_collapsed_line():
    assert "if (isWorkerMsg) _makeWorkerResultCollapsible(wrap, r, textRaw, metadata);" in RENDERER
    helper = RENDERER.split("function _makeWorkerResultCollapsible", 1)[1].split("\n}\n", 1)[0]
    assert "isCardOpen(key)" in helper and "setCardOpen(key, nowOpen)" in helper
    assert ".msg.msg-worker-result.collapsed > .body { display: none; }" in STYLE


def test_control_room_shows_a_sub_agents_conversation():
    detail = AGENTS.split("function renderDetail()", 1)[1].split("function steerLogHtml", 1)[0]
    # Only a sub-agent (a chat with a parent) gets the tab.
    assert "const isSubAgent = !!r.parent_session;" in detail
    assert "['conversation', 'Conversation']" in detail
    assert "tab === 'conversation' ? conversationHtml(r)" in detail
    # Read from the chat's own saved history, tool events included.
    assert "/api/history/${encodeURIComponent(sid)}?limit=${CONVERSATION_LIMIT}" in detail
    assert "meta.tool_events" in detail
    assert 'data-ag="convo-refresh"' in detail and "act === 'convo-refresh'" in AGENTS
