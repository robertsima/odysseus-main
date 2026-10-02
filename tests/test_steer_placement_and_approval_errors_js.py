"""chat.js: where a steered bubble lands, why it waits, and approval send errors.

Reported 2026-10-02: the second and later steers stacked at the first steer's
position in the chat, and sending an approval the server refused showed "This
model doesn't support agent tools - switched to Chat mode" and flipped the mode
toggle, because every approval refusal contains the word "tool" and the parser
read "message" where FastAPI sends "detail".

Like test_steering_composer_lifecycle_js.py, these run the real source slices
under Node with a small DOM stand-in.
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


def _slice(start_marker: str, end_marker: str) -> str:
    src = CHAT.read_text(encoding="utf-8")
    start = src.index(start_marker)
    return src[start:src.index(end_marker, start)]


def _lifecycle_source() -> str:
    return _slice("function _createSteeredBubble", "  /**\n   * Send a message typed while a turn is still running.")


# A tiny DOM: elements with a parent, ordered children, classes and dataset.
_DOM = """
class El {
  constructor(name, classes = []) {
    this.name = name; this.parentNode = null; this.children = []; this.dataset = {};
    this.classes = new Set(classes); this.isConnected = true; this.title = 'pending';
    const owner = this;
    this.classList = {
      contains: (c) => owner.classes.has(c),
      remove: (c) => owner.classes.delete(c),
      add: (c) => owner.classes.add(c),
    };
    this.role = { textContent: '' };
  }
  appendChild(node) { this._detach(node); node.parentNode = this; this.children.push(node); return node; }
  insertBefore(node, ref) {
    this._detach(node);
    node.parentNode = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(node); else this.children.splice(at, 0, node);
    return node;
  }
  _detach(node) {
    if (node.parentNode) node.parentNode.children = node.parentNode.children.filter((c) => c !== node);
  }
  get nextSibling() {
    const kids = this.parentNode ? this.parentNode.children : [];
    return kids[kids.indexOf(this) + 1] || null;
  }
  removeAttribute() {}
  querySelector(sel) { return sel === '.role' ? this.role : null; }
  remove() { this._detach(this); this.parentNode = null; this.isConnected = false; }
}
"""


def test_consecutive_steers_land_after_the_replies_that_came_before_them():
    out = _run(
        """
const _steeredBubbles = new Map();
const _steerStatusTimers = new Map();
const API_BASE = '';
const sessionModule = { getCurrentSessionId: () => 'chat-1' };
const uiModule = { showError() {}, showToast() {} };
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.fetch = async () => ({ ok: false });
"""
        + _DOM
        + _lifecycle_source().replace("export ", "")
        + """
const transcript = new El('transcript');
const host = new El('host', ['chat-queued-bubble-host']);
const user0 = new El('user0'); const reply1 = new El('reply1');
const round2 = new El('round2', ['msg-ai', 'streaming']);
[user0, reply1, round2, host].forEach((n) => transcript.appendChild(n));

function steer(id) {
  const bubble = new El(id, ['msg-user-steered']);
  bubble.dataset.steerId = id; bubble.dataset.steerState = 'queued';
  host.appendChild(bubble);
  _trackedSteers('chat-1').set(id, bubble);
  return bubble;
}
steer('steer-1');
// steer_applied for the first: the stream is writing into round2.
_handleSteerApplied('chat-1', { steer_id: 'steer-1', text: 'one', round: 2 }, round2);
// The turn goes on: round2 finishes, round3 is appended after the host (the
// stream appends new rounds to the end of the chat).
round2.classes.delete('streaming');
const round3 = new El('round3', ['msg-ai', 'streaming']);
transcript.appendChild(round3);
steer('steer-2');
_handleSteerApplied('chat-1', { steer_id: 'steer-2', text: 'two', round: 3 }, round3);
// A third, with no anchor handed in (the status poll path): the newest
// streaming holder is found from the document.
const round4 = new El('round4', ['msg-ai', 'streaming']);
round3.classes.delete('streaming');
transcript.appendChild(round4);
globalThis.document = { querySelectorAll: () => [round4] };
steer('steer-3');
_applySteeredBubbleState('chat-1', { id: 'steer-3', state: 'injected' });

console.log(JSON.stringify({
  order: transcript.children.filter((c) => c !== host).map((c) => c.name),
  promoted: transcript.children.filter((c) => c.name.startsWith('steer')).every((c) => !c.classes.has('msg-user-steered')),
  left: _steeredBubbles.size,
}));
"""
    )
    assert out["order"] == ["user0", "reply1", "steer-1", "round2", "steer-2", "round3", "steer-3", "round4"]
    assert out["promoted"] is True
    assert out["left"] == 0


def test_a_queued_steer_says_what_it_is_waiting_for():
    out = _run(
        """
const _steeredBubbles = new Map();
const _steerStatusTimers = new Map();
const API_BASE = '';
const sessionModule = { getCurrentSessionId: () => 'chat-1' };
const uiModule = { showError() {}, showToast() {} };
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
"""
        + _lifecycle_source().replace("export ", "")
        + """
const idle = _steerWaitHint('chat-1');
_trackLiveActivity('chat-1', { type: 'tool_start', tool: 'bash', started_at: Date.now() / 1000 - 125 });
const busy = _steerWaitHint('chat-1');
_trackLiveActivity('chat-1', { type: 'tool_output', tool: 'bash' });
const after = _steerWaitHint('chat-1');
console.log(JSON.stringify({ idle, busy, after }));
"""
    )
    assert "model's current step" in out["idle"]["title"]
    assert out["busy"]["pill"].startswith("After bash")
    assert "2m 5s" in out["busy"]["pill"] or "2m 6s" in out["busy"]["pill"]
    assert "bash" in out["busy"]["title"]
    # Once the tool ends the hint falls back to the model's step.
    assert out["after"] == out["idle"]


def test_a_steer_kept_across_an_ended_turn_is_queued_as_the_next_message():
    out = _run(
        """
const _steeredBubbles = new Map();
const _steerStatusTimers = new Map();
const API_BASE = '';
const queued = []; const toasts = []; const errors = [];
const sessionModule = { getCurrentSessionId: () => 'chat-1' };
const uiModule = { showError: (m) => errors.push(m), showToast: (m) => toasts.push(m), el: () => ({ value: '' }) };
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
function _queueAgentRequest(text) { queued.push(text); return true; }
"""
        + _lifecycle_source().replace("export ", "")
        + """
_handleSteerDropped('chat-1', {
  carry: true, reason: 'the agent asked you a question',
  messages: [{ id: 's1', text: 'also check the logs', kind: 'user' }],
});
const input = { value: '' };
uiModule.el = () => input;
_handleSteerDropped('chat-1', { carry: false, messages: [{ id: 's2', text: 'too late', kind: 'user' }] });
console.log(JSON.stringify({ queued, toasts, errors, composer: input.value }));
"""
    )
    assert out["queued"] == ["also check the logs"]
    assert "asked you a question" in out["toasts"][0]
    # Without carry the old behavior stands: text back in the composer, with an apology.
    assert out["composer"] == "too late"
    assert len(out["errors"]) == 1


def test_the_queued_host_is_moved_to_the_end_each_time_it_is_used():
    out = _run(
        _DOM
        + """
const chatBox = new El('chat');
const host = new El('host', ['chat-queued-bubble-host']);
let _queuedBubbleHost = null;
globalThis.document = {
  getElementById: (id) => (id === 'chat-history' ? chatBox : null),
  createElement: () => new El('made'),
};
"""
        + _slice("function _ensureQueuedBubbleHost", "  function _createQueuedBubble").replace("export ", "")
        + """
chatBox.appendChild(host); _queuedBubbleHost = host;
chatBox.appendChild(new El('round2'));
const used = _ensureQueuedBubbleHost();
console.log(JSON.stringify({ last: chatBox.children[chatBox.children.length - 1].name, same: used === host }));
"""
    )
    assert out == {"last": "host", "same": True}


# ── approval send errors ───────────────────────────────────────────────────

def _approval_helpers() -> str:
    return _slice("  /** The message a failed chat request carries", "  function _submitToolApprovalWhenIdle")


def test_an_approval_refusal_shows_the_servers_reason_and_is_not_a_tool_support_error():
    out = _run(
        """
const calls = [];
const chatRenderer = {
  renderAskUserCard: (payload) => { calls.push(['render', payload.approval_id]); return { card: true }; },
  lapseApprovalCard: (card, state, text) => calls.push(['lapse', state, text]),
};
const errors = [];
const uiModule = { showError: (m) => errors.push(m) };
"""
        + _approval_helpers()
        + """
const detail = 'This tool approval is invalid, expired, or belongs to another thread.';
const body = JSON.stringify({ detail });
const text = _serverErrorText(body);
const approval = { approval_id: 'a1', payload: { approval_id: 'a1' } };
_settleFailedApproval(approval, 409, text);
const afterConflict = calls.splice(0);
_settleFailedApproval(approval, 503, 'upstream down');
const afterOutage = calls.splice(0);
_settleFailedApproval({ approval_id: 'a2', payload: null }, 409, text);
console.log(JSON.stringify({
  text,
  refusalIsToolSupport: _isToolSupportError(text),
  nested: _serverErrorText(JSON.stringify({ detail: 'x {"error": {"message": "inner reason"}}' })),
  validation: _serverErrorText(JSON.stringify({ detail: [{ msg: 'field required' }] })),
  legacy: _serverErrorText('{"message":"old shape"}'),
  afterConflict, afterOutage, errors,
}));
"""
    )
    assert out["text"] == "This tool approval is invalid, expired, or belongs to another thread."
    assert out["refusalIsToolSupport"] is False
    assert out["nested"] == "inner reason"
    assert out["validation"] == "field required"
    assert out["legacy"] == "old shape"
    # A refusal the server states as final brings the card back, lapsed, with its reason.
    assert out["afterConflict"] == [["render", "a1"], ["lapse", "expired", out["text"]]]
    # A transient failure brings it back live, so it can be clicked again.
    assert out["afterOutage"] == [["render", "a1"]]
    # With nothing to restore the user still hears why.
    assert out["errors"] == [out["text"]]


def test_only_a_provider_saying_tools_are_unsupported_counts_as_a_tool_support_error():
    out = _run(
        _approval_helpers().replace("chatRenderer", "chatRendererUnused")
        + """
const cases = {
  genuine: [
    'This model does not support tools',
    "registry.ollama.ai/library/llama2 does not support tools",
    'tools are not supported with this model',
    'function calling is not supported',
    'auto tool choice requires --enable-auto-tool-choice and --tool-call-parser',
  ],
  other: [
    'This tool approval is invalid, expired, or belongs to another thread.',
    'Tool budget reached',
    'A newer message replaced this approval',
    'Automatic retry failed',
    'Error 500',
  ],
};
console.log(JSON.stringify({
  genuine: cases.genuine.map(_isToolSupportError),
  other: cases.other.map(_isToolSupportError),
}));
"""
    )
    assert all(out["genuine"]), out
    assert not any(out["other"]), out


def test_an_approval_send_failure_never_switches_the_mode_toggle():
    src = CHAT.read_text(encoding="utf-8")
    start = src.index("if (approvalForSend) {\n          _settleFailedApproval")
    block = src[start:src.index("typewriterInto(holder.querySelector('.body'), errText)", start)]
    approval_branch, _, support_branch = block.partition("} else if (_isToolSupportError(errText))")
    assert "mode-chat-btn" not in approval_branch
    assert "mode-chat-btn" in support_branch, "genuine tool-support errors still switch to Chat mode"
    # The card is restored when the request never got an answer, too.
    assert "!_approvalDelivered" in src and "_settleFailedApproval(approvalForSend, 0, '')" in src
