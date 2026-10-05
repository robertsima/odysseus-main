"""Pin buildReplyAllCc (static/js/emailLibrary/replyRecipients.js) against a
non-string To/Cc. Driven through `node --input-type=module`; skips without node.
"""
import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module
from tests import REPO_ROOT

_REPO = REPO_ROOT
_HELPER = _REPO / "static" / "js" / "emailLibrary" / "replyRecipients.js"
_HAS_NODE = shutil.which("node") is not None


def _cc(data, mine):
    js = f"""
    import {{ buildReplyAllCc }} from '{module_url(_HELPER)}';
    console.log(JSON.stringify(buildReplyAllCc({json.dumps(data)}, {json.dumps(mine)})));
    """
    proc = run_module(js)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_build_reply_all_cc_tolerates_non_string_fields():
    # data.to / data.cc come from a JSON message blob and are not always
    # strings; the old (s || "").split crashed on a non-string To.
    out = _cc({"to": 123, "cc": "a@x.com, b@x.com"}, "me@x.com")
    assert out == "a@x.com, b@x.com"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_build_reply_all_cc_still_excludes_self():
    out = _cc({"to": "me@x.com, a@x.com", "cc": ""}, "me@x.com")
    assert out == "a@x.com"
