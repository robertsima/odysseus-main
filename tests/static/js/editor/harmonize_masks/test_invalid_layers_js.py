"""Pin harmonize mask helpers against invalid layer lists.

Driven through `node --input-type=module`; skips without node.
"""
import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module
from tests import REPO_ROOT

_REPO = REPO_ROOT
_HELPER = _REPO / "static" / "js" / "editor" / "harmonize-masks.js"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_layer_union_alpha_returns_null_for_non_array_layers():
    js = f"""
    import {{ layerUnionAlpha, seamMask, layerBodyMask }} from '{module_url(_HELPER)}';
    console.log(JSON.stringify([
      layerUnionAlpha(10, 10, null),
      seamMask(10, 10, {{"bad": true}}),
      layerBodyMask(10, 10, "bad")
    ]));
    """
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == [None, None, None]
