"""Pin _matchesCombo (static/js/keyboard-shortcuts.js) against a non-string
keybind. Driven through `node --input-type=module` (same approach as
tests/test_markdown_table_row_js.py); skips when `node` is missing.

Regression: keybinds are merged from the server response of
`/api/auth/settings` (`{ ..._defaultKeybinds, ...s.keybinds }`). A corrupt
or malformed `keybinds` value (e.g. a number instead of "ctrl+k") reached
`combo.split('+')` and threw "combo.split is not a function", breaking the
whole keydown handler. The guard treats any non-string combo as "no match".
"""
import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module

_REPO = Path(__file__).resolve().parent.parent
_MOD = _REPO / "static" / "js" / "keyboard-shortcuts.js"
_HAS_NODE = shutil.which("node") is not None

_EVENT = "{key:'k',ctrlKey:false,altKey:false,shiftKey:false,metaKey:false}"


def _match(combo_js):
    js = f"""
    import {{ _matchesCombo }} from '{module_url(_MOD)}';
    console.log(JSON.stringify(_matchesCombo({_EVENT}, {combo_js})));
    """
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_non_string_combo_is_no_match():
    assert _match("123") is False
    assert _match("{}") is False
    assert _match("null") is False


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_matching_combo_still_fires():
    assert _match("'k'") is True
