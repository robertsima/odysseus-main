import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module

_REPO = Path(__file__).resolve().parent.parent
_MODULE = _REPO / "static" / "js" / "presets.js"
_HAS_NODE = shutil.which("node") is not None


def _load_values():
    js = f"""
    globalThis.localStorage = {{
      getItem(key) {{
        return {{
          broken: '{{',
          list: '[]',
          object: '{{"session":"Socrates"}}',
        }}[key] ?? null;
      }},
    }};
    const presets = await import('{module_url(_MODULE)}');
    console.log(JSON.stringify({{
      brokenArray: presets.loadStoredArray('broken'),
      wrongArray: presets.loadStoredArray('object'),
      brokenObject: presets.loadStoredObject('broken'),
      wrongObject: presets.loadStoredObject('list'),
      object: presets.loadStoredObject('object'),
    }}));
    """
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_preset_storage_helpers_fall_back_for_bad_values():
    assert _load_values() == {
        "brokenArray": [],
        "wrongArray": [],
        "brokenObject": {},
        "wrongObject": {},
        "object": {"session": "Socrates"},
    }
