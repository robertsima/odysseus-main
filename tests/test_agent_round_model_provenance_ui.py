"""Saved Agent rounds must render and bill with actual per-round provenance."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


_SOURCE = (
    Path(__file__).resolve().parents[1] / "static" / "js" / "chatRenderer.js"
).read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None


def _function_source(name):
    match = re.search(
        rf"^(?:export )?function {name}\(.*?^\}}",
        _SOURCE,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"{name} not found"
    return match.group(0).replace("export function", "function", 1)


def _run_node(source):
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_agent_usage_buckets_sum_only_billable_answering_routes():
    source = "\n".join([
        "let currentUrl = '';",
        "function _currentEndpointUrl() { return currentUrl; }",
        "function isCostTrackedEndpoint(url) { return url === 'paid'; }",
        "function getModelCost(_model, inputTokens, outputTokens) { return (inputTokens + outputTokens) / 1000; }",
        _function_source("_billableCost"),
        _function_source("_metricsBillableCost"),
        "const paidSelected = {usage_buckets: [",
        "  {model: 'selected', input_tokens: 100, output_tokens: 10, endpoint_cost_tracked: true},",
        "  {model: 'local-fallback', input_tokens: 200, output_tokens: 20, endpoint_cost_tracked: false},",
        "]};",
        "const localSelected = {usage_buckets: [",
        "  {model: 'selected', input_tokens: 100, output_tokens: 10, endpoint_cost_tracked: false},",
        "  {model: 'paid-fallback', input_tokens: 200, output_tokens: 20, endpoint_cost_tracked: true},",
        "]};",
        "currentUrl = 'local';",
        "const paidToLocal = _metricsBillableCost(paidSelected, 'final', 300, 30);",
        "currentUrl = 'paid';",
        "const localToPaid = _metricsBillableCost(localSelected, 'final', 300, 30);",
        "console.log(JSON.stringify({paidToLocal, localToPaid}));",
    ])

    assert _run_node(source) == {"paidToLocal": 0.11, "localToPaid": 0.22}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_force_answer_synthesis_segment_is_included_in_fallback_cost():
    source = "\n".join([
        "function _currentEndpointUrl() { return 'local-selected'; }",
        "function isCostTrackedEndpoint() { return false; }",
        "function getModelCost(_model, inputTokens, outputTokens) { return (inputTokens + outputTokens) / 1000; }",
        _function_source("_billableCost"),
        _function_source("_metricsBillableCost"),
        "const metrics = {usage_buckets: [",
        "  {round: 6, model: 'paid-fallback', input_tokens: 100, output_tokens: 0, endpoint_cost_tracked: true},",
        "  {round: 6, model: 'paid-fallback', input_tokens: 80, output_tokens: 20, endpoint_cost_tracked: true},",
        "]};",
        "console.log(JSON.stringify({cost: _metricsBillableCost(metrics, 'paid-fallback', 180, 20)}));",
    ])

    assert _run_node(source) == {"cost": 0.2}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_repeated_live_metrics_render_records_session_cost_once():
    source = "\n".join([
        "const _COST_KEY = 'ody-session-cost';",
        "const state = {};",
        "const localStorage = {",
        "  getItem(key) { return state[key] || null; },",
        "  setItem(key, value) { state[key] = value; },",
        "};",
        "const window = {sessionModule: {getCurrentSessionId() { return 'session'; }}};",
        "function updateSessionCostUI() {}",
        "function _currentEndpointUrl() { return 'local'; }",
        "function isCostTrackedEndpoint(url) { return url === 'paid'; }",
        "function getModelCost(_model, inputTokens, outputTokens) { return (inputTokens + outputTokens) / 1000; }",
        _function_source("_billableCost"),
        _function_source("_metricsBillableCost"),
        _function_source("recordSessionMetricsCost"),
        "const metrics = {model: 'paid-model', input_tokens: 100, output_tokens: 10, endpoint_cost_tracked: true};",
        "recordSessionMetricsCost(metrics);",
        "recordSessionMetricsCost(metrics);",
        "console.log(JSON.stringify({cost: JSON.parse(state[_COST_KEY]).session, recorded: metrics._costRecorded}));",
    ])

    assert _run_node(source) == {"cost": 0.11, "recorded": True}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_replayed_metrics_use_run_identity_for_durable_cost_deduplication():
    source = "\n".join([
        "const _COST_KEY = 'ody-session-cost';",
        "const _COST_RUNS_KEY = 'ody-session-cost-runs';",
        "const _MAX_COST_RUNS_PER_SESSION = 256;",
        "const state = {};",
        "const localStorage = {",
        "  getItem(key) { return state[key] || null; },",
        "  setItem(key, value) { state[key] = value; },",
        "};",
        "const window = {sessionModule: {getCurrentSessionId() { return 'session'; }}};",
        "function updateSessionCostUI() {}",
        "function _currentEndpointUrl() { return 'local'; }",
        "function isCostTrackedEndpoint(url) { return url === 'paid'; }",
        "function getModelCost(_model, inputTokens, outputTokens) { return (inputTokens + outputTokens) / 1000; }",
        _function_source("_billableCost"),
        _function_source("_metricsBillableCost"),
        _function_source("recordSessionMetricsCost"),
        _function_source("getSessionCost"),
        "const firstObject = {model: 'paid-model', input_tokens: 100, output_tokens: 10, endpoint_cost_tracked: true, _costRecordId: 'run-1'};",
        "const replayedObject = {...firstObject};",
        "recordSessionMetricsCost(firstObject);",
        "recordSessionMetricsCost(replayedObject);",
        "console.log(JSON.stringify({cost: getSessionCost('session'), runs: JSON.parse(state[_COST_RUNS_KEY]).session}));",
    ])

    assert _run_node(source) == {"cost": 0.11, "runs": {"run-1": 0.11}}


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_run_cost_ledger_sums_segments_and_updates_repeated_segment_metrics():
    source = "\n".join([
        "const _COST_KEY = 'ody-session-cost';",
        "const _COST_RUNS_KEY = 'ody-session-cost-runs';",
        "const _MAX_COST_RUNS_PER_SESSION = 256;",
        "const state = {};",
        "const localStorage = {",
        "  getItem(key) { return state[key] || null; },",
        "  setItem(key, value) { state[key] = value; },",
        "};",
        "const window = {sessionModule: {getCurrentSessionId() { return 'session'; }}};",
        "function updateSessionCostUI() {}",
        "function _currentEndpointUrl() { return 'paid'; }",
        "function isCostTrackedEndpoint() { return true; }",
        "function getModelCost(_model, inputTokens, outputTokens) { return (inputTokens + outputTokens) / 1000; }",
        _function_source("_billableCost"),
        _function_source("_metricsBillableCost"),
        _function_source("recordSessionMetricsCost"),
        _function_source("getSessionCost"),
        "recordSessionMetricsCost({model: 'student', input_tokens: 100, output_tokens: 10, _costRecordId: 'run:primary'});",
        "recordSessionMetricsCost({model: 'student', input_tokens: 120, output_tokens: 20, _costRecordId: 'run:primary'});",
        "recordSessionMetricsCost({model: 'teacher', input_tokens: 200, output_tokens: 30, _costRecordId: 'run:teacher'});",
        "console.log(JSON.stringify({cost: getSessionCost('session'), runs: JSON.parse(state[_COST_RUNS_KEY]).session}));",
    ])

    assert _run_node(source) == {
        "cost": 0.37,
        "runs": {"run:primary": 0.14, "run:teacher": 0.23},
    }


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_local_selected_endpoint_does_not_erase_paid_fallback_ledger():
    source = "\n".join([
        "const _COST_KEY = 'ody-session-cost';",
        "const _COST_RUNS_KEY = 'ody-session-cost-runs';",
        "const state = {'ody-session-cost': JSON.stringify({session: 0.125})};",
        "const localStorage = {",
        "  getItem(key) { return state[key] || null; },",
        "  setItem(key, value) { state[key] = value; },",
        "};",
        "const badge = {style: {}, textContent: ''};",
        "const document = {getElementById() { return badge; }};",
        "const window = {sessionModule: {getCurrentSessionId() { return 'session'; }, getCurrentEndpointUrl() { return 'local'; }}};",
        _function_source("getSessionCost"),
        _function_source("updateSessionCostUI"),
        "updateSessionCostUI();",
        "console.log(JSON.stringify({stored: JSON.parse(state[_COST_KEY]).session, display: badge.style.display, text: badge.textContent}));",
    ])

    assert _run_node(source) == {
        "stored": 0.125,
        "display": "",
        "text": "$0.125",
    }
