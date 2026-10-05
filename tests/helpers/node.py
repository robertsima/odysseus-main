"""Run JavaScript modules under node from Python tests on any platform.

Two things differ on Windows. Node's ESM loader rejects a bare absolute path
such as ``D:/repo/static/js/x.js`` with ERR_UNSUPPORTED_ESM_URL_SCHEME, so an
import specifier must be a ``file://`` URL. And ``subprocess`` decodes output
with the locale code page (cp1252) unless told otherwise, which garbles
characters such as the middle dot or the ellipsis that node prints as UTF-8.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def module_url(path: str | Path) -> str:
    """Return the ``file://`` URL node's ESM loader accepts for ``path``."""
    return Path(path).resolve().as_uri()


def run_module(source: str, *, cwd: str | Path = REPO, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run ``source`` as an ES module read from stdin, decoding output as UTF-8."""
    return subprocess.run(
        ["node", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        timeout=timeout,
    )
