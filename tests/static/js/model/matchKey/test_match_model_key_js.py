"""Pin matchModelKey (static/js/model/matchKey.js).

Driven through `node --input-type=module` (same approach as test_compare_js.py);
skips when `node` is not installed.

Regression: model name -> info/pricing lookups returned the FIRST substring
match, so "gpt-4o-mini" matched the shorter "gpt-4o" key and was billed at
gpt-4o rates (~16x) with the wrong context window.
"""
import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "model" / "matchKey.js"
_HAS_NODE = shutil.which("node") is not None

_KEYS = ["gpt-4o", "gpt-4o-mini", "gpt-4", "o1", "o1-mini", "o1-pro", "o3", "o3-mini"]


def _match(name):
    js = (
        f"import {{ matchModelKey }} from '{module_url(_HELPER)}';"
        f"console.log(JSON.stringify(matchModelKey({json.dumps(name)}, {json.dumps(_KEYS)})));"
    )
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_prefers_longest_specific_key():
    assert _match("gpt-4o-mini") == "gpt-4o-mini"
    assert _match("o1-mini") == "o1-mini"
    assert _match("o1-pro") == "o1-pro"
    assert _match("o3-mini") == "o3-mini"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_base_model_and_unknown():
    assert _match("gpt-4o-2024-08-06") == "gpt-4o"
    assert _match("some-unknown-model") is None
