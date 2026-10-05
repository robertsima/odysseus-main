"""Source contract for live multi-round fallback attribution."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


CHAT_JS = Path("static/js/chat.js").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None


def _resume_function_source():
    body = CHAT_JS.split("export async function resumeStream", 1)[1].split(
        "export function checkBackgroundStream", 1
    )[0]
    return "async function resumeStream" + body.rstrip()


def _run_node(source):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_detached_resume_surfaces_fallback_then_provider_alias_before_reload():
    source = "\n".join([
        "import { applyModelRouteEventState } from './static/js/chatModelProvenance.js';",
        "class Element {",
        "  constructor(tag = 'div') { this.tag = tag; this.children = []; this.parentNode = null; this.style = {}; this.textContent = ''; this._html = ''; }",
        "  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }",
        "  remove() { if (!this.parentNode) return; this.parentNode.children = this.parentNode.children.filter(c => c !== this); this.parentNode = null; }",
        "  set innerHTML(value) {",
        "    this._html = value;",
        "    if (value.includes('stream-content')) {",
        "      this._role = new Element('div'); this._role.parentNode = this;",
        "      this._body = new Element('div'); this._body.parentNode = this;",
        "      this._content = new Element('div'); this._body.appendChild(this._content);",
        "    }",
        "  }",
        "  get innerHTML() { return this._html; }",
        "  querySelector(selector) { if (selector === '.role') return this._role || null; if (selector === '.body') return this._body || null; if (selector === '.stream-content') return this._content || null; return null; }",
        "}",
        "const box = new Element('main');",
        "const document = { getElementById(id) { return id === 'chat-history' ? box : null; }, createElement(tag) { return new Element(tag); }, querySelector() { return null; } };",
        "const window = {};",
        "let selectCalls = 0; const labels = []; const toasts = [];",
        "const sessionModule = { getSessions() { return [{id: 's1', model: 'selected-model'}]; }, getCurrentSessionId() { return 's1'; }, selectSession() { selectCalls += 1; }, loadSessions() {} };",
        "const uiModule = { esc(value) { return String(value); }, scrollHistory() {}, showToast(value) { toasts.push(value); } };",
        "const spinnerModule = { create() { return { element: null, createElement() { this.element = new Element('spinner'); return this.element; }, start() {}, destroy() { if (this.element) this.element.remove(); } }; } };",
        "const markdownModule = { normalizeThinkingMarkup(v) { return v; }, mdToHtml(v) { return v; }, squashOutsideCode(v) { return v; } };",
        "const documentModule = null; const chatRenderer = { recordSessionMetricsCost() {}, addMessage() {} };",
        "const _resumingStreams = new Set(); const _streamRunIds = new Map(); const API_BASE = '';",
        "const _activeStreams = new Map(); function updateSubmitButton() {} function _releaseResumedComposer() {} function _drainQueuedAgentRequests() {}",
        "function _createResumeActivity() { return { event() {}, stop() {} }; } function _trackLiveActivity() {}",
        "function hasActiveStream() { return false; } function _shortModel(v) { return v; } function _applyModelColor() {}",
        "function _setRoleModelLabel(role, requested, actual) { labels.push({requested, actual}); role.textContent = requested + ' -> ' + actual; }",
        "function _streamDisplayText(v) { return v; } function _showDocumentWritingStatus() {} function _finishDocumentWritingStatus() {} function _metricsCostRecordId() { return 'run'; }",
        "const events = [",
        "  'data: {\"type\":\"fallback\",\"selected_model\":\"selected-model\",\"answered_by\":\"fallback-model\",\"reason\":\"429\"}\\n\\n',",
        "  'data: {\"type\":\"model_actual\",\"model\":\"provider/fallback-alias\"}\\n\\n',",
        "  'data: {\"delta\":\"hello\"}\\n\\n',",
        "  'data: [DONE]\\n\\n',",
        "].join('');",
        "const encoded = new TextEncoder().encode(events); let reads = 0;",
        "const reader = { async read() { return reads++ === 0 ? {done:false, value:encoded} : {done:true}; }, async cancel() {} };",
        "async function fetch() { return { ok:true, body:{getReader(){return reader;}}, headers:{get(){return 'run-1';}} }; }",
        _resume_function_source(),
        "await resumeStream('s1');",
        "console.log(JSON.stringify({labels, toasts, selectCalls, holderCount: box.children.length}));",
    ])

    assert _run_node(source) == {
        "labels": [
            {"requested": "selected-model", "actual": "fallback-model"},
            {"requested": "selected-model", "actual": "provider/fallback-alias"},
        ],
        "toasts": ["Fallback: selected-model failed — answered by fallback-model"],
        "selectCalls": 1,
        "holderCount": 0,
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_detached_resume_renders_preoutput_error_without_empty_reload():
    source = "\n".join([
        "import { createTerminalStreamError } from './static/js/chatStreamErrors.js';",
        "class Element {",
        "  constructor(tag = 'div') { this.tag = tag; this.children = []; this.parentNode = null; this.style = {}; this.textContent = ''; this._html = ''; }",
        "  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }",
        "  remove() { if (!this.parentNode) return; this.parentNode.children = this.parentNode.children.filter(c => c !== this); this.parentNode = null; }",
        "  set innerHTML(value) {",
        "    this._html = value;",
        "    if (value.includes('stream-content')) {",
        "      this._role = new Element('div'); this._role.parentNode = this;",
        "      this._body = new Element('div'); this._body.parentNode = this;",
        "      this._content = new Element('div'); this._body.appendChild(this._content);",
        "    }",
        "  }",
        "  get innerHTML() { return this._html; }",
        "  querySelector(selector) { if (selector === '.role') return this._role || null; if (selector === '.body') return this._body || null; if (selector === '.stream-content') return this._content || null; return null; }",
        "}",
        "const box = new Element('main');",
        "const document = { getElementById(id) { return id === 'chat-history' ? box : null; }, createElement(tag) { return new Element(tag); }, querySelector() { return null; } };",
        "const window = {};",
        "let selectCalls = 0;",
        "const sessionModule = { getSessions() { return [{id: 's1', model: 'selected'}]; }, getCurrentSessionId() { return 's1'; }, selectSession() { selectCalls += 1; }, loadSessions() {} };",
        "const uiModule = { esc(value) { return String(value); }, scrollHistory() {} };",
        "const spinnerModule = { create() { return { element: null, createElement() { this.element = new Element('spinner'); return this.element; }, start() {}, destroy() { if (this.element) this.element.remove(); } }; } };",
        "const markdownModule = { normalizeThinkingMarkup(v) { return v; }, mdToHtml(v) { return v; }, squashOutsideCode(v) { return v; } };",
        "const documentModule = null;",
        "const chatRenderer = { recordSessionMetricsCost() {}, addMessage() {} };",
        "const _resumingStreams = new Set(); const _streamRunIds = new Map(); const API_BASE = '';",
        "const _activeStreams = new Map(); function updateSubmitButton() {} function _releaseResumedComposer() {} function _drainQueuedAgentRequests() {}",
        "function _createResumeActivity() { return { event() {}, stop() {} }; } function _trackLiveActivity() {}",
        "function hasActiveStream() { return false; } function _shortModel(v) { return v; } function _applyModelColor() {}",
        "function _streamDisplayText(v) { return v; } function _showDocumentWritingStatus() {} function _finishDocumentWritingStatus() {} function _metricsCostRecordId() { return 'run'; }",
        "const encoded = new TextEncoder().encode('event: error\\ndata: {\"status\":401,\"error\":\"invalid key <img src=x>\"}\\n\\n');",
        "let reads = 0; const reader = { async read() { return reads++ === 0 ? {done:false, value:encoded} : {done:true}; }, async cancel() {} };",
        "async function fetch() { return { ok:true, body:{getReader(){return reader;}}, headers:{get(){return 'run-1';}} }; }",
        _resume_function_source(),
        "const result = await resumeStream('s1');",
        "const holder = box.children[0]; const errorNode = holder && holder._content.children.find(node => node.textContent.startsWith('[Error:'));",
        "console.log(JSON.stringify({result, selectCalls, holderCount: box.children.length, errorText: errorNode && errorNode.textContent}));",
    ])

    assert _run_node(source) == {
        "result": True,
        "selectCalls": 0,
        "holderCount": 1,
        "errorText": "[Error: invalid key <img src=x>]",
    }


