"""Behavioral coverage for chat.js's ephemeral steering composer chips.

The chat module itself is intentionally DOM-heavy, so these tests run the
small lifecycle section from the real source under Node with a tiny DOM/fetch
stand-in. That exercises the state transitions rather than merely checking
that particular source strings exist.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
CHAT = ROOT / "static" / "js" / "chat.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def _run(script: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, text=True,
        capture_output=True, cwd=ROOT, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _lifecycle_source() -> str:
    src = CHAT.read_text(encoding="utf-8")
    start = src.index("function _trackedSteers")
    end = src.index("  /**\n   * Send a message typed while a turn is still running.", start)
    return src[start:end]


def _queue_source() -> str:
    src = CHAT.read_text(encoding="utf-8")
    start = src.index("export function queueStreamingComposerRequest")
    end = src.index("  function _drainQueuedAgentRequests", start)
    return src[start:end].replace("export function queueStreamingComposerRequest", "function queueStreamingComposerRequest", 1)


def test_lifecycle_poll_is_marked_and_terminal_events_remove_only_the_ephemeral_chip():
    lifecycle = _lifecycle_source()
    out = _run(
        """
const _steeredBubbles = new Map();
const _steerStatusTimers = new Map();
const API_BASE = '';
const errors = [];
const uiModule = { showError: (message) => errors.push(message) };
const timers = [];
globalThis.setTimeout = (fn, delay) => { timers.push({ fn, delay }); return timers.length; };
globalThis.clearTimeout = () => {};
const seenFetches = [];
globalThis.fetch = async (_url, options) => {
  seenFetches.push(options);
  return { ok: true, json: async () => ({ messages: [{ id: 'one', state: 'injected' }] }) };
};
LIFECYCLE
function transcriptBubble() {
  const transcript = { insertBefore(node, before) { node.parentNode = this; node.promotedBefore = before; } };
  const host = { parentNode: transcript, classList: { contains: (name) => name === 'chat-queued-bubble-host' } };
  const role = { textContent: '' };
  return {
    isConnected: true, parentNode: host, dataset: { steerId: 'one', steerState: 'queued' }, removed: false,
    removedClasses: [], role, title: 'pending',
    classList: { remove(name) { this.owner.removedClasses.push(name); }, owner: null },
    removeAttribute(name) { delete this[name]; },
    querySelector: (selector) => selector === '.role' ? role : null,
    remove() { this.removed = true; this.isConnected = false; },
  };
}
const bubble = transcriptBubble();
bubble.classList.owner = bubble;
_trackedSteers('chat-1').set('one', bubble);
_scheduleSteerStatusCheck('chat-1');
await timers.shift().fn();

// The stream path uses the same lifecycle transition and therefore does not
// need to wait for a reconciliation request.
const streamBubble = transcriptBubble();
streamBubble.classList.owner = streamBubble;
_trackedSteers('chat-1').set('two', streamBubble);
_handleSteerApplied('chat-1', { steer_id: 'two', round: 3 });

// A history redraw can remove a bubble before the server has emitted its row.
// Cleanup must stop tracking it even when the poll response is empty.
const detached = { isConnected: false };
_trackedSteers('chat-2').set('gone', detached);
_scheduleSteerStatusCheck('chat-2');
await timers.shift().fn();
console.log(JSON.stringify({
  pollHeader: seenFetches[0].headers['X-Odysseus-Poll'],
  polledPromoted: bubble.parentNode === bubble.promotedBefore.parentNode && bubble.role.textContent === 'You' && bubble.removedClasses.includes('msg-user-steered'),
  streamPromoted: streamBubble.parentNode === streamBubble.promotedBefore.parentNode && streamBubble.role.textContent === 'You',
  chat1Tracked: _steeredBubbles.has('chat-1'),
  chat2Tracked: _steeredBubbles.has('chat-2'),
  errors,
}));
""".replace("LIFECYCLE", lifecycle)
    )
    assert out["pollHeader"] == "1"
    assert out["polledPromoted"] is True
    assert out["streamPromoted"] is True
    assert out["chat1Tracked"] is False
    assert out["chat2Tracked"] is False
    assert out["errors"] == []


def test_late_steer_post_cannot_draw_or_queue_into_a_newly_selected_chat():
    queue = _queue_source()
    out = _run(
        """
let isStreaming = true;
let selected = 'chat-old';
const input = { value: 'focus on the migration', dispatchEvent() {} };
const sessionModule = { getCurrentSessionId: () => selected };
const uiModule = { el: () => input, autoResize() {}, showToast() {}, showError() {} };
const fileHandlerModule = { getPendingCount: () => 0 };
const API_BASE = '';
let created = 0;
let tracked = 0;
let queued = 0;
const _createSteeredBubble = () => { created += 1; return {}; };
const _trackSteeredBubble = () => { tracked += 1; };
const _queueAgentRequest = () => { queued += 1; };
globalThis.window = { _updateSendBtnIcon() {} };
globalThis.Event = class Event { constructor(type) { this.type = type; } };
let respond;
globalThis.fetch = () => new Promise((resolve) => { respond = resolve; });
QUEUE
queueStreamingComposerRequest();
selected = 'chat-new';
respond({ ok: true, json: async () => ({ id: 'steer-late' }) });
await new Promise((resolve) => setTimeout(resolve, 0));
console.log(JSON.stringify({ created, tracked, queued, input: input.value }));
""".replace("QUEUE", queue)
    )
    assert out == {"created": 0, "tracked": 0, "queued": 0, "input": ""}
