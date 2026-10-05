"""Agent cards go back in time order when the chat re-renders.

When a background run in the open chat ends, the chat reloads to show the
worker's result and the reply written after it. The live agent cards were
then re-appended below everything, so the reply that followed the workers'
completion appeared ABOVE the cards showing them run and finish (2026-09-29).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKBENCH = ROOT / "static" / "js" / "workbench.js"

HARNESS = r"""
const vm = require('vm');
const src = process.argv[1];
// The 2026-09-29 chat: the turn that launched the worker, the user's next
// message and its reply, then the worker's result and the follow-up reply.
const msgs = [
  { id: 'ask',       cls: ['msg', 'msg-user'], ts: 100 },
  { id: 'launch',    cls: ['msg', 'msg-ai'],   ts: 110 },
  { id: 'next-ask',  cls: ['msg', 'msg-user'], ts: 120 },
  { id: 'next-ai',   cls: ['msg', 'msg-ai'],   ts: 130 },
  { id: 'result',    cls: ['msg', 'msg-user', 'msg-worker-result'], ts: 140, from: 'worker-chat' },
  { id: 'follow-up', cls: ['msg', 'msg-ai'],   ts: 150 },
].map((m) => ({ id: m.id, cls: new Set(m.cls),
  dataset: { ts: new Date(m.ts * 1000).toISOString(), ...(m.from ? { fromSession: m.from } : {}) } }));
const hist = { querySelectorAll(sel) {
  if (sel === '.msg-worker-result') return msgs.filter((m) => m.cls.has('msg-worker-result'));
  if (sel === '.msg[data-ts]') return msgs.filter((m) => m.cls.has('msg') && m.dataset.ts);
  throw new Error('unexpected selector ' + sel);
} };
const state = { sessionId: 'parent', agentRuns: new Map([
  ['worker-feed-only', { summary: { parent_session: 'parent', target_session: 'worker-chat' }, started_at: 105 }],
]) };
const ctx = { state, Date, Number, String };
vm.runInNewContext(src + '\nthis.cardAnchor = cardAnchor;', ctx);
const at = (run) => { const m = ctx.cardAnchor(hist, run); return m ? m.id : null; };
console.log(JSON.stringify({
  worker: at({ run_id: 'w', started_at: 105, data: { parent_session: 'parent', target_session: 'worker-chat' } }),
  worker_known_from_feed: at({ run_id: 'worker-feed-only', started_at: 105, data: {} }),
  continuation: at({ run_id: 'c', started_at: 141, data: { target_session: 'worker-chat' } }),
  in_turn_job: at({ run_id: 'j', started_at: 125, data: {} }),
  still_running: at({ run_id: 'r', started_at: 160, data: {} }),
  unknown: at(undefined),
}));
"""


def _card_anchor_source() -> str:
    text = WORKBENCH.read_text(encoding="utf-8")
    start = text.index("function cardAnchor(hist, run)")
    end = text.index("\nfunction ", start + 1)
    return text[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_cards_go_back_where_their_runs_happened():
    out = subprocess.run(["node", "-e", HARNESS, _card_anchor_source()],
                         capture_output=True, text=True, timeout=30, check=True)
    placed = json.loads(out.stdout)
    # A worker's card sits just above the result it handed back, so the reply
    # written after that result comes after the card.
    assert placed["worker"] == "result"
    assert placed["worker_known_from_feed"] == "result"
    # The chat's own continuation (and anything else run inside a turn) sits
    # above the reply that turn wrote.
    assert placed["continuation"] == "follow-up"
    assert placed["in_turn_job"] == "next-ai"
    # Nothing saved after it yet: at the end, as while it runs.
    assert placed["still_running"] is None
    assert placed["unknown"] is None
