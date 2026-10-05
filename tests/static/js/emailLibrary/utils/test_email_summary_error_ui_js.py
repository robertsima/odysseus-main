import json
import shutil
from pathlib import Path

import pytest

from tests.helpers.node import module_url, run_module


_REPO = Path(__file__).resolve().parent.parent
_UTILS = module_url(_REPO / "static" / "js" / "emailLibrary" / "utils.js")
_HAS_NODE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


def test_email_summary_renderer_ignores_untrusted_provider_error_text():
    secret = (
        "endpoint=https://private.example.internal/v1 provider=ollama "
        "model=private-model response_body=private-response "
        "Authorization: Bearer token-secret-value"
    )
    script = f"""
      import {{ _renderEmailSummaryError }} from '{_UTILS}';
      const host = {{
        ownerDocument: {{
          createElement() {{ return {{ style: {{}}, textContent: '' }}; }},
        }},
        replaceChildren(node) {{ this.child = node; }},
      }};
      _renderEmailSummaryError(host, {{
        error_code: 'email_summary_unavailable',
        error: {json.dumps(secret)},
      }});
      console.log(JSON.stringify({{
        text: host.child.textContent,
        color: host.child.style.color,
      }}));
    """

    proc = run_module(script)

    assert proc.returncode == 0, proc.stderr
    rendered = json.loads(proc.stdout)
    assert rendered == {"text": "Failed to summarize", "color": "var(--red)"}
    assert secret not in proc.stdout
