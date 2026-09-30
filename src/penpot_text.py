"""Measure and wrap text the way a browser will draw it.

Penpot draws a text shape from its stored ``position-data`` (one record per
drawn line: x, y, width, height, text, font). Shapes written through the HTTP
API have none, and Penpot's viewer and exporter then draw NOTHING for them; a
wrong width is just as bad because the line is stretched to fit it. The web
app fills the field in by measuring in the DOM, so this module does the same
with headless Chromium (already shipped for the Browser tool), loading the
Google font Penpot will use so the widths are the real ones.

If no browser is available the fallback is an estimate, flagged as such.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import quote_plus

MEASURE_TIMEOUT_S = 60


@dataclass
class TextSpec:
    text: str
    family: str
    weight: str
    italic: bool
    size: float
    letter_spacing: float
    max_width: Optional[float]

    def key(self) -> tuple:
        return (self.text, self.family, self.weight, self.italic, self.size,
                self.letter_spacing, self.max_width)


@dataclass
class Line:
    text: str
    width: float


@dataclass
class Measured:
    lines: List[Line]
    exact: bool


_CACHE: Dict[tuple, Measured] = {}


def browser_executable() -> str:
    from src.builtin_mcp import _find_browser_executable
    exe = _find_browser_executable()
    if exe:
        return exe
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for cand in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if os.path.isfile(cand):
            return cand
    return ""


def estimate(spec: TextSpec) -> Measured:
    """Width guess from the size alone (used only when nothing can measure)."""
    heavy = spec.weight in ("700", "800", "900")
    per = spec.size * (0.58 if heavy else 0.52) + spec.letter_spacing
    lines: List[Line] = []
    for para in spec.text.split("\n"):
        if not spec.max_width:
            lines.append(Line(para, len(para) * per))
            continue
        cur = ""
        for word in para.split(" "):
            trial = f"{cur} {word}" if cur else word
            if cur and len(trial) * per > spec.max_width:
                lines.append(Line(cur, len(cur) * per))
                cur = word
            else:
                cur = trial
        lines.append(Line(cur, len(cur) * per))
    return Measured(lines, exact=False)


_PAGE = """<!doctype html><meta charset="utf-8">
%(links)s
<pre id="out">pending</pre>
<script>
const specs = %(specs)s;
(async () => {
  const out = [];
  const fontOf = s => `${s.italic ? 'italic ' : ''}${s.weight} ${s.size}px "${s.family}"`;
  for (const s of specs) { try { await document.fonts.load(fontOf(s), s.text); } catch (e) {} }
  try { await document.fonts.ready; } catch (e) {}
  const ctx = document.createElement('canvas').getContext('2d');
  for (const s of specs) {
    const font = fontOf(s);
    ctx.font = font;
    ctx.letterSpacing = s.ls + 'px';
    const w = t => ctx.measureText(t).width;
    const lines = [];
    for (const para of s.text.split('\\n')) {
      if (!s.max) { lines.push({text: para, width: w(para)}); continue; }
      let cur = '';
      for (const word of para.split(' ')) {
        const trial = cur ? cur + ' ' + word : word;
        if (cur && w(trial) > s.max) { lines.push({text: cur, width: w(cur)}); cur = word; }
        else cur = trial;
      }
      lines.push({text: cur, width: w(cur)});
    }
    out.push({lines, exact: document.fonts.check(font, s.text)});
  }
  document.getElementById('out').textContent = JSON.stringify(out);
})();
</script>"""


def _font_link(family: str, weight: str, italic: bool) -> str:
    fam = quote_plus(family)
    axis = f"ital,wght@{1 if italic else 0},{weight}" if italic else f"wght@{weight}"
    return f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={fam}:{axis}&display=block">'


async def measure(specs: List[TextSpec]) -> List[Measured]:
    """Measure every spec in one browser launch. Never raises: falls back to
    :func:`estimate` (``exact=False``) when the browser or fonts are missing."""
    results: List[Optional[Measured]] = [_CACHE.get(s.key()) for s in specs]
    todo = [i for i, r in enumerate(results) if r is None]
    if todo:
        fresh = await _measure_in_browser([specs[i] for i in todo])
        for i, m in zip(todo, fresh):
            results[i] = m
            if m.exact:
                _CACHE[specs[i].key()] = m
    return [r for r in results if r is not None]


async def _measure_in_browser(specs: List[TextSpec]) -> List[Measured]:
    exe = browser_executable()
    if not exe:
        return [estimate(s) for s in specs]
    payload = [{"text": s.text, "family": s.family, "weight": s.weight, "italic": s.italic,
                "size": s.size, "ls": s.letter_spacing, "max": s.max_width} for s in specs]
    links = "\n".join(sorted({_font_link(s.family, s.weight, s.italic) for s in specs}))
    page = _PAGE % {"links": links, "specs": json.dumps(payload).replace("</", "<\\/")}
    with tempfile.TemporaryDirectory(prefix="penpot-measure-") as tmp:
        html = os.path.join(tmp, "m.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(page)
        args = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--dump-dom",
                "--virtual-time-budget=20000", f"--user-data-dir={os.path.join(tmp, 'profile')}",
                "file:///" + html.replace("\\", "/").lstrip("/")]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=MEASURE_TIMEOUT_S)
        except (asyncio.TimeoutError, OSError):
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return [estimate(s) for s in specs]
    match = re.search(r'<pre id="out">(.*?)</pre>', stdout.decode("utf-8", "replace"), re.S)
    if not match or match.group(1).strip() in ("", "pending"):
        return [estimate(s) for s in specs]
    try:
        data = json.loads(_unescape(match.group(1)))
        return [Measured([Line(l["text"], float(l["width"])) for l in d["lines"]], bool(d["exact"]))
                for d in data]
    except (ValueError, KeyError, TypeError):
        return [estimate(s) for s in specs]


def _unescape(text: str) -> str:
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&amp;", "&"))
