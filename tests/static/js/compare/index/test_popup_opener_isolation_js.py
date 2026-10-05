import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_compare_print_popup_detaches_opener_before_document_write():
    src = _source("static/js/compare/index.js")
    match = re.search(
        r"function _exportPrint\(\) \{(?P<body>.*?)w\.document\.close\(\);",
        src,
        re.S,
    )

    assert match
    body = match.group("body")
    assert "w.opener = null" in body
    assert body.index("w.opener = null") < body.index("w.document.write(html)")
