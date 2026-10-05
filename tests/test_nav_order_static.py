from pathlib import Path
import json
import shutil
import subprocess

import pytest


NAV = Path(__file__).parents[1] / "static" / "js" / "navOrder.js"


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
