"""Pin computeSnap (static/js/editor/snap.js) against a non-array otherLayers.
Driven through `node --input-type=module`; skips without node.
"""
import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "editor" / "snap.js"
_HAS_NODE = shutil.which("node") is not None


def _snap(other_layers):
    js = f"""
    import {{ computeSnap }} from '{module_url(_HELPER)}';
    const layer = {{ id: 'L1', canvas: {{ width: 100, height: 50 }} }};
    const ctx = {{ zoom: 1, canvasW: 800, canvasH: 600, otherLayers: {json.dumps(other_layers)} }};
    console.log(JSON.stringify(computeSnap(layer, 10, 10, ctx)));
    """
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_compute_snap_tolerates_non_array_other_layers():
    # ctx.otherLayers should be an array, but during init / error recovery it
    # can be missing or wrong-typed; the old `for...of` threw on a non-iterable.
    r = _snap(123)
    assert r["x"] == 10 and r["y"] == 10 and r["guides"] == []


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_compute_snap_tolerates_missing_layer_or_context():
    js = f"""
    import {{ computeSnap }} from '{module_url(_HELPER)}';
    console.log(JSON.stringify([
      computeSnap(null, 10, 20, {{ zoom: 1, canvasW: 800, canvasH: 600 }}),
      computeSnap({{ id: 'L1' }}, 11, 21, {{ zoom: 1, canvasW: 800, canvasH: 600 }}),
      computeSnap({{ id: 'L1', canvas: {{ width: 100, height: 50 }} }}, 12, 22, null)
    ]));
    """
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == [
        {"x": 10, "y": 20, "guides": []},
        {"x": 11, "y": 21, "guides": []},
        {"x": 12, "y": 22, "guides": []},
    ]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_compute_snap_still_snaps_to_a_layer_edge():
    other = [{"id": "L2", "visible": True, "offset": {"x": 12, "y": 300},
              "canvas": {"width": 100, "height": 50}}]
    r = _snap(other)
    assert r["x"] == 12
