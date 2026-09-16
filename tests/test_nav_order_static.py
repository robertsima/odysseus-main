from pathlib import Path
import json
import shutil
import subprocess

import pytest


NAV = Path(__file__).parents[1] / "static" / "js" / "navOrder.js"


def source():
    return NAV.read_text(encoding="utf-8")


def test_nav_order_is_a_standalone_module_with_shared_persistence_key():
    text = source()
    assert "export const NAV_ORDER_KEY = 'odysseus-nav-order-v1';" in text
    assert "export function initNavOrder" in text
    assert "localStorage" in text
    assert "applyNavOrder(order, doc)" in text


def test_tool_items_are_movable_and_core_controls_are_excluded():
    text = source()
    for item in (
        "rail-calendar", "rail-compare", "rail-cookbook", "rail-research",
        "rail-gallery", "rail-archive", "rail-memory", "rail-agents",
        "rail-workbench", "rail-notes", "rail-tasks", "rail-theme", "rail-email",
        "tool-calendar-btn", "tool-compare-btn", "tool-cookbook-btn",
        "tool-research-btn", "tool-gallery-btn", "tool-library-btn",
        "tool-memory-btn", "tool-agents-btn", "tool-workbench-btn",
        "tool-notes-btn", "tool-tasks-btn", "tool-theme-btn", "tool-lotus-btn",
    ):
        assert item in text
    for excluded in ("rail-new-session", "rail-delete-session", "rail-chats", "rail-documents", "rail-settings"):
        # Excluded controls may be mentioned in comments or event selectors,
        # but must not be part of NAV_ITEMS.
        nav_items = text.split("const KEYS", 1)[0]
        assert excluded not in nav_items


def test_keyboard_drag_and_reset_affordances_are_present():
    text = source()
    assert "event.altKey" in text
    assert "ArrowUp" in text and "ArrowDown" in text
    assert "dragstart" in text and "dragover" in text and "drop" in text
    assert "nav-order-context-menu" in text
    assert "Reset navigation order" in text
    assert "export function resetNavOrder" in text


def test_dragging_a_tool_does_not_also_activate_it():
    text = source()
    assert "_suppressClickUntil = Date.now() + 300" in text
    assert "event.stopImmediatePropagation()" in text


def test_hidden_nodes_are_reordered_without_changing_visibility():
    text = source()
    assert "Existing hidden" in text
    assert ".style.display" not in text
    assert ".hidden" not in text


def test_desired_order_is_inserted_into_current_dom_slots():
    text = source()
    assert "const desired = ids.map" in text
    assert "const current = Array.from(container.children)" in text
    assert "const slots = current.map" in text
    assert "desired.forEach((node, index)" in text


def test_reordering_moves_existing_nodes_and_preserves_fixed_slots():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    script = """
import { applyNavOrder } from MODULE;
const nodes = new Map();
const doc = {
  getElementById: id => nodes.get(id),
  createComment: () => ({ comment: true, remove() { this.parentNode.remove(this); } }),
};
function container(id, ids) {
  const root = { id, ownerDocument: doc, all: [],
    get children() { return this.all.filter(n => !n.comment); },
    insertBefore(n, before) { this.all.splice(this.all.indexOf(before), 0, n); n.parentNode = this; },
    remove(n) { this.all.splice(this.all.indexOf(n), 1); n.parentNode = null; },
  };
  nodes.set(id, root);
  root.all = ids.map(id => {
    const n = { id, parentNode: root, hidden: id.includes('calendar'), remove() { this.parentNode.remove(this); } };
    nodes.set(id, n); return n;
  });
  return root;
}
const rail = container('icon-rail', ['fixed-top', 'rail-calendar', 'fixed-middle', 'rail-agents', 'rail-notes', 'fixed-bottom']);
const tools = container('tools-section', ['tool-calendar-btn', 'tool-agents-btn', 'tool-notes-btn']);
const calendar = nodes.get('rail-calendar');
applyNavOrder(['notes', 'agents', 'calendar'], doc);
if (rail.children.map(n => n.id).join() !== 'fixed-top,rail-notes,fixed-middle,rail-agents,rail-calendar,fixed-bottom') throw Error('rail order/slots');
if (tools.children.map(n => n.id).join() !== 'tool-notes-btn,tool-agents-btn,tool-calendar-btn') throw Error('sidebar order');
if (nodes.get('rail-calendar') !== calendar || !calendar.hidden) throw Error('node identity/visibility');
""".replace("MODULE", json.dumps(NAV.resolve().as_uri()))
    result = subprocess.run([node, "--input-type=module"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
