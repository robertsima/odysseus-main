"""SVG -> Penpot path content.

Penpot has no "import this SVG" call in its HTTP API: the web app parses a
dropped SVG in the browser and writes shapes. The Penpot MCP tools only create
rectangles, circles, text and frames, so on 2026-09-30 a designer agent asked
for a helmet logo could do nothing but stack circles. This module is the
missing half: it turns SVG markup (an icon-library icon, a logo, a traced
drawing) into the segment lists Penpot stores for a ``path`` shape, so the
artwork arrives as real, editable, recolourable vectors.

Pure functions, no I/O: :mod:`src.penpot_studio` owns the network and the file
changes.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Matrix = Tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# Refuse pathological markup instead of spending minutes converting it.
MAX_SVG_CHARS = 2_000_000
MAX_SEGMENTS = 60_000

_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_CMD = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")


try:  # pragma: no cover - which parser is present depends on the install
    from defusedxml.ElementTree import fromstring as _fromstring
except ImportError:  # pragma: no cover
    _fromstring = ET.fromstring


class SvgError(ValueError):
    """The markup cannot be converted; the message says why."""


def _mul(m: Matrix, n: Matrix) -> Matrix:
    """``m`` applied after ``n`` (SVG transform lists compose left to right)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def parse_transform(text: Optional[str]) -> Matrix:
    """SVG ``transform`` attribute -> matrix. Unknown functions are ignored."""
    result = IDENTITY
    if not text:
        return result
    for name, args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", text):
        v = [float(n) for n in _NUM.findall(args)]
        name = name.lower()
        m: Optional[Matrix] = None
        if name == "matrix" and len(v) == 6:
            m = (v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            m = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            m = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            rot: Matrix = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                m = _mul(_mul((1, 0, 0, 1, v[1], v[2]), rot), (1, 0, 0, 1, -v[1], -v[2]))
            else:
                m = rot
        elif name == "skewx" and v:
            m = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewy" and v:
            m = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        if m:
            result = _mul(result, m)
    return result


# ---------------------------------------------------------------------------
# Path data
# ---------------------------------------------------------------------------

def _tokens(d: str) -> Iterable[Tuple[str, List[float]]]:
    """Yield ``(command, numbers)`` with arc flags split out correctly.

    Minified icon sets write arcs as ``a2 2 0 011 1``: the two flags are single
    characters with no separator, so a plain number regex would read ``011``
    as one number.
    """
    pos = 0
    n = len(d)
    while pos < n:
        m = _CMD.search(d, pos)
        if not m:
            break
        cmd = m.group(0)
        nxt = _CMD.search(d, m.end())
        body = d[m.end(): nxt.start() if nxt else n]
        pos = nxt.start() if nxt else n
        if cmd in "Aa":
            nums: List[float] = []
            i, L = 0, len(body)
            while i < L:
                group: List[float] = []
                for slot in range(7):
                    while i < L and body[i] in " ,\t\r\n":
                        i += 1
                    if i >= L:
                        break
                    if slot in (3, 4):
                        if body[i] not in "01":
                            raise SvgError("malformed arc flag in path data")
                        group.append(float(body[i]))
                        i += 1
                    else:
                        mm = _NUM.match(body, i)
                        if not mm:
                            raise SvgError("malformed number in path data")
                        group.append(float(mm.group(0)))
                        i = mm.end()
                if len(group) == 7:
                    nums.extend(group)
                elif group:
                    raise SvgError("incomplete arc in path data")
            yield cmd, nums
        else:
            yield cmd, [float(x) for x in _NUM.findall(body)]


def _arc_to_cubics(x1: float, y1: float, rx: float, ry: float, phi: float,
                   fa: int, fs: int, x2: float, y2: float) -> List[Tuple[float, ...]]:
    """SVG elliptical arc -> cubic Béziers ``(c1x, c1y, c2x, c2y, x, y)``."""
    if (x1 == x2 and y1 == y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [(x1, y1, x2, y2, x2, y2)]
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        mag = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / mag))) if mag else 0.0
        return -a if ux * vy - uy * vx < 0 else a

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dth > 0:
        dth -= 2 * math.pi
    elif fs and dth < 0:
        dth += 2 * math.pi
    count = max(1, int(math.ceil(abs(dth) / (math.pi / 2) - 1e-9)))
    step = dth / count
    t = 4 / 3 * math.tan(step / 4)
    out: List[Tuple[float, ...]] = []
    a = th1
    for _ in range(count):
        ca, sa = math.cos(a), math.sin(a)
        cb, sb = math.cos(a + step), math.sin(a + step)
        pts = [
            (ca - t * sa, sa + t * ca),
            (cb + t * sb, sb - t * cb),
            (cb, sb),
        ]
        flat: List[float] = []
        for ux, uy in pts:
            px, py = rx * ux, ry * uy
            flat.extend((cp * px - sp * py + cx, sp * px + cp * py + cy))
        out.append(tuple(flat))
        a += step
    return out


# Segment = ("M"|"L"|"C"|"Z", coords)
Seg = Tuple[str, Tuple[float, ...]]


def path_segments(d: str) -> List[Seg]:
    """Path ``d`` -> absolute M/L/C/Z segments (Q, S, T, A, H, V normalised)."""
    segs: List[Seg] = []
    x = y = sx = sy = 0.0
    last_c: Optional[Tuple[float, float]] = None  # previous cubic control
    last_q: Optional[Tuple[float, float]] = None  # previous quad control
    started = False
    for cmd, v in _tokens(d):
        rel = cmd.islower()
        c = cmd.upper()
        if c == "Z":
            if started:
                segs.append(("Z", ()))
            x, y = sx, sy
            last_c = last_q = None
            continue
        size = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}[c]
        if not v or len(v) % size:
            raise SvgError(f"path command {cmd!r} has the wrong number of values")
        first = True
        for i in range(0, len(v), size):
            a = v[i: i + size]
            if c == "M":
                nx, ny = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                if first:
                    segs.append(("M", (nx, ny)))
                    sx, sy = nx, ny
                    started = True
                else:  # extra pairs after a moveto are implicit linetos
                    segs.append(("L", (nx, ny)))
                x, y = nx, ny
                last_c = last_q = None
            elif c == "L":
                nx, ny = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                segs.append(("L", (nx, ny)))
                x, y = nx, ny
                last_c = last_q = None
            elif c == "H":
                x = x + a[0] if rel else a[0]
                segs.append(("L", (x, y)))
                last_c = last_q = None
            elif c == "V":
                y = y + a[0] if rel else a[0]
                segs.append(("L", (x, y)))
                last_c = last_q = None
            elif c == "C":
                p = [a[0] + x, a[1] + y, a[2] + x, a[3] + y, a[4] + x, a[5] + y] if rel else list(a)
                segs.append(("C", tuple(p)))
                last_c = (p[2], p[3])
                last_q = None
                x, y = p[4], p[5]
            elif c == "S":
                p = [a[0] + x, a[1] + y, a[2] + x, a[3] + y] if rel else list(a)
                c1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
                segs.append(("C", (c1[0], c1[1], p[0], p[1], p[2], p[3])))
                last_c = (p[0], p[1])
                last_q = None
                x, y = p[2], p[3]
            elif c == "Q":
                p = [a[0] + x, a[1] + y, a[2] + x, a[3] + y] if rel else list(a)
                segs.append(("C", _quad(x, y, p[0], p[1], p[2], p[3])))
                last_q = (p[0], p[1])
                last_c = None
                x, y = p[2], p[3]
            elif c == "T":
                nx, ny = (x + a[0], y + a[1]) if rel else (a[0], a[1])
                q = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                segs.append(("C", _quad(x, y, q[0], q[1], nx, ny)))
                last_q = q
                last_c = None
                x, y = nx, ny
            elif c == "A":
                nx, ny = (x + a[5], y + a[6]) if rel else (a[5], a[6])
                for cub in _arc_to_cubics(x, y, a[0], a[1], a[2], int(a[3]), int(a[4]), nx, ny):
                    segs.append(("C", cub))
                x, y = nx, ny
                last_c = last_q = None
            first = False
        if len(segs) > MAX_SEGMENTS:
            raise SvgError("drawing has too many segments to import")
    return segs


def _quad(x0: float, y0: float, qx: float, qy: float, x: float, y: float) -> Tuple[float, ...]:
    return (
        x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
        x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y),
        x, y,
    )


def transform_segments(segs: Sequence[Seg], m: Matrix) -> List[Seg]:
    if m == IDENTITY:
        return list(segs)
    out: List[Seg] = []
    for cmd, pts in segs:
        flat: List[float] = []
        for i in range(0, len(pts), 2):
            flat.extend(_apply(m, pts[i], pts[i + 1]))
        out.append((cmd, tuple(flat)))
    return out


def segments_bbox(segs: Sequence[Seg]) -> Tuple[float, float, float, float]:
    """Exact ``(min_x, min_y, max_x, max_y)`` including Bézier extrema."""
    xs: List[float] = []
    ys: List[float] = []
    cx = cy = 0.0
    for cmd, p in segs:
        if cmd in ("M", "L"):
            cx, cy = p
            xs.append(cx)
            ys.append(cy)
        elif cmd == "C":
            for axis, store, start in ((0, xs, cx), (1, ys, cy)):
                p0, p1, p2, p3 = start, p[axis], p[2 + axis], p[4 + axis]
                store.append(p3)
                # derivative of the cubic is a quadratic: a t^2 + b t + c
                a = -p0 + 3 * p1 - 3 * p2 + p3
                b = 2 * (p0 - 2 * p1 + p2)
                c = p1 - p0
                roots: List[float] = []
                if abs(a) < 1e-12:
                    if abs(b) > 1e-12:
                        roots.append(-c / b)
                else:
                    disc = b * b - 4 * a * c
                    if disc >= 0:
                        sq = math.sqrt(disc)
                        roots.extend(((-b + sq) / (2 * a), (-b - sq) / (2 * a)))
                for t in roots:
                    if 0 < t < 1:
                        u = 1 - t
                        store.append(u ** 3 * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t ** 3 * p3)
            cx, cy = p[4], p[5]
    if not xs:
        raise SvgError("the drawing has no geometry")
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------

def _f(value: Optional[str], default: float = 0.0) -> float:
    if value is None:
        return default
    m = _NUM.search(value)
    return float(m.group(0)) if m else default


def _ellipse(cx: float, cy: float, rx: float, ry: float) -> List[Seg]:
    k = 0.5522847498307936
    return [
        ("M", (cx + rx, cy)),
        ("C", (cx + rx, cy + k * ry, cx + k * rx, cy + ry, cx, cy + ry)),
        ("C", (cx - k * rx, cy + ry, cx - rx, cy + k * ry, cx - rx, cy)),
        ("C", (cx - rx, cy - k * ry, cx - k * rx, cy - ry, cx, cy - ry)),
        ("C", (cx + k * rx, cy - ry, cx + rx, cy - k * ry, cx + rx, cy)),
        ("Z", ()),
    ]


def _rect(x: float, y: float, w: float, h: float, rx: float, ry: float) -> List[Seg]:
    rx, ry = min(rx, w / 2), min(ry, h / 2)
    if rx <= 0 or ry <= 0:
        return [("M", (x, y)), ("L", (x + w, y)), ("L", (x + w, y + h)), ("L", (x, y + h)), ("Z", ())]
    k = 0.5522847498307936
    return [
        ("M", (x + rx, y)), ("L", (x + w - rx, y)),
        ("C", (x + w - rx + k * rx, y, x + w, y + ry - k * ry, x + w, y + ry)),
        ("L", (x + w, y + h - ry)),
        ("C", (x + w, y + h - ry + k * ry, x + w - rx + k * rx, y + h, x + w - rx, y + h)),
        ("L", (x + rx, y + h)),
        ("C", (x + rx - k * rx, y + h, x, y + h - ry + k * ry, x, y + h - ry)),
        ("L", (x, y + ry)),
        ("C", (x, y + ry - k * ry, x + rx - k * rx, y, x + rx, y)),
        ("Z", ()),
    ]


def _points(text: str, close: bool) -> List[Seg]:
    v = [float(n) for n in _NUM.findall(text or "")]
    pts = list(zip(v[0::2], v[1::2]))
    if len(pts) < 2:
        return []
    segs: List[Seg] = [("M", pts[0])] + [("L", p) for p in pts[1:]]
    if close:
        segs.append(("Z", ()))
    return segs


_NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
    "yellow": "#ffff00", "orange": "#ffa500", "gray": "#808080", "grey": "#808080", "purple": "#800080",
    "pink": "#ffc0cb", "brown": "#a52a2a", "cyan": "#00ffff", "magenta": "#ff00ff", "silver": "#c0c0c0",
    "gold": "#ffd700", "navy": "#000080", "maroon": "#800000", "teal": "#008080", "lime": "#00ff00",
}


def resolve_color(value: Optional[str], gradients: Dict[str, str], notes: List[str]) -> Optional[str]:
    """CSS colour -> ``#rrggbb``, ``currentColor`` or None (paint nothing).

    Gradients are flattened to the average of their end stops (Penpot paths can
    carry gradients, but icons that use them survive as flat colour, which is
    what a designer recolouring them wants); the flattening is reported.
    """
    if value is None:
        return None
    v = value.strip()
    low = v.lower()
    if low in ("none", "transparent", ""):
        return None
    if low == "currentcolor":
        return "currentColor"
    if re.fullmatch(r"#[0-9a-f]{3}|#[0-9a-f]{6}", low):
        return low if len(low) == 7 else "#" + "".join(ch * 2 for ch in low[1:])
    m = re.fullmatch(r"rgba?\(([^)]*)\)", low)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")[:3]]
        try:
            nums = [round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p)) for p in parts]
            return "#%02x%02x%02x" % tuple(max(0, min(255, n)) for n in nums)
        except ValueError:
            pass
    if low in _NAMED:
        return _NAMED[low]
    m = re.fullmatch(r"url\(\s*['\"]?#([^)'\"\s]+)['\"]?\s*\)", v)
    if m:
        note = "gradient fills were flattened to a solid colour"
        if note not in notes:
            notes.append(note)
        return gradients.get(m.group(1), "#808080")
    note = f"colour {value!r} is not supported and was drawn grey"
    if note not in notes:
        notes.append(note)
    return "#808080"


def _gradient_colors(root: ET.Element) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) not in ("lineargradient", "radialgradient") or "id" not in el.attrib:
            continue
        stops = []
        for stop in el:
            if _local(stop.tag) != "stop":
                continue
            style = dict(part.split(":", 1) for part in (stop.attrib.get("style") or "").split(";") if ":" in part)
            col = resolve_color(style.get("stop-color") or stop.attrib.get("stop-color"), {}, [])
            if col and col != "currentColor":
                stops.append(col)
        if stops:
            a, b = stops[0], stops[-1]
            out[el.attrib["id"]] = "#" + "".join(
                "%02x" % round((int(a[i:i + 2], 16) + int(b[i:i + 2], 16)) / 2) for i in (1, 3, 5))
    return out


@dataclass
class Paint:
    fill: Optional[str] = "currentColor"  # SVG's default fill is black
    fill_opacity: float = 1.0
    stroke: Optional[str] = None
    stroke_opacity: float = 1.0
    stroke_width: float = 1.0
    linecap: str = "butt"
    fill_rule: str = "nonzero"
    opacity: float = 1.0

    def key(self) -> tuple:
        return (self.fill, self.fill_opacity, self.stroke, self.stroke_opacity,
                self.stroke_width, self.linecap, self.fill_rule, self.opacity)


@dataclass
class PathGroup:
    """Segments sharing one paint: becomes one Penpot ``path`` shape."""
    paint: Paint
    segments: List[Seg] = field(default_factory=list)


@dataclass
class ParsedSvg:
    groups: List[PathGroup]
    width: float   # viewBox size, the drawing's natural coordinate space
    height: float
    min_x: float
    min_y: float
    notes: List[str] = field(default_factory=list)


_STYLE_ATTRS = ("fill", "fill-opacity", "stroke", "stroke-opacity", "stroke-width",
                "stroke-linecap", "fill-rule", "opacity")


def _style(el: ET.Element) -> Dict[str, str]:
    out = {k: el.attrib[k] for k in _STYLE_ATTRS if k in el.attrib}
    for part in (el.attrib.get("style") or "").split(";"):
        if ":" in part:
            k, _, v = part.partition(":")
            if k.strip() in _STYLE_ATTRS:
                out[k.strip()] = v.strip()
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_svg(markup: str) -> ParsedSvg:
    """Convert SVG markup into paint-grouped absolute path segments.

    Coordinates stay in the SVG's own space; :func:`fit` scales and places
    them. ``<use>``/``<defs>``/gradients/masks/text/images are not supported
    (and silently skipped, since icon sets and logos rarely need them); a
    drawing with nothing convertible raises :class:`SvgError`.
    """
    if not isinstance(markup, str) or "<svg" not in markup:
        raise SvgError("not SVG markup (no <svg> element)")
    if len(markup) > MAX_SVG_CHARS:
        raise SvgError("SVG is too large to import")
    # Icons and logos never need a DTD; refusing it closes entity-expansion and
    # external-entity tricks whether or not defusedxml is installed.
    if "<!DOCTYPE" in markup.upper() or "<!ENTITY" in markup.upper():
        raise SvgError("SVG with a DOCTYPE or entity declarations is refused")
    try:
        root = _fromstring(markup)
    except ET.ParseError as exc:
        raise SvgError(f"SVG is not well-formed XML: {exc}") from exc
    if _local(root.tag) != "svg":
        raise SvgError("root element is not <svg>")

    vb = re.findall(_NUM, root.attrib.get("viewBox", ""))
    if len(vb) == 4:
        min_x, min_y, width, height = (float(n) for n in vb)
    else:
        min_x = min_y = 0.0
        width = _f(root.attrib.get("width"), 24.0)
        height = _f(root.attrib.get("height"), width)
    if width <= 0 or height <= 0:
        raise SvgError("SVG has no usable size")

    groups: Dict[tuple, PathGroup] = {}
    order: List[tuple] = []
    notes: List[str] = []
    gradients = _gradient_colors(root)

    def add(segs: List[Seg], paint: Paint) -> None:
        if not segs:
            return
        k = paint.key()
        if k not in groups:
            groups[k] = PathGroup(paint)
            order.append(k)
        groups[k].segments.extend(segs)

    def walk(el: ET.Element, matrix: Matrix, inherited: Dict[str, str]) -> None:
        tag = _local(el.tag)
        if tag in ("defs", "clippath", "mask", "symbol", "title", "desc", "metadata",
                   "style", "lineargradient", "radialgradient", "pattern", "filter"):
            return
        style = {**inherited, **_style(el)}
        m = _mul(matrix, parse_transform(el.attrib.get("transform")))
        if style.get("display") == "none" or el.attrib.get("display") == "none":
            return
        segs: List[Seg] = []
        if tag == "path":
            segs = path_segments(el.attrib.get("d", ""))
        elif tag == "rect":
            w, h = _f(el.attrib.get("width")), _f(el.attrib.get("height"))
            rx = _f(el.attrib.get("rx"), _f(el.attrib.get("ry")))
            ry = _f(el.attrib.get("ry"), rx)
            if w > 0 and h > 0:
                segs = _rect(_f(el.attrib.get("x")), _f(el.attrib.get("y")), w, h, rx, ry)
        elif tag == "circle":
            r = _f(el.attrib.get("r"))
            if r > 0:
                segs = _ellipse(_f(el.attrib.get("cx")), _f(el.attrib.get("cy")), r, r)
        elif tag == "ellipse":
            rx, ry = _f(el.attrib.get("rx")), _f(el.attrib.get("ry"))
            if rx > 0 and ry > 0:
                segs = _ellipse(_f(el.attrib.get("cx")), _f(el.attrib.get("cy")), rx, ry)
        elif tag == "line":
            segs = [("M", (_f(el.attrib.get("x1")), _f(el.attrib.get("y1")))),
                    ("L", (_f(el.attrib.get("x2")), _f(el.attrib.get("y2"))))]
        elif tag in ("polygon", "polyline"):
            segs = _points(el.attrib.get("points", ""), tag == "polygon")
        if segs:
            fill = style.get("fill", "currentColor")
            stroke = style.get("stroke")
            scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0
            fill_c = resolve_color(fill, gradients, notes)
            stroke_c = resolve_color(stroke, gradients, notes)
            paint = Paint(
                fill=fill_c,
                fill_opacity=_f(style.get("fill-opacity"), 1.0),
                stroke=stroke_c,
                stroke_opacity=_f(style.get("stroke-opacity"), 1.0),
                stroke_width=_f(style.get("stroke-width"), 1.0) * scale,
                linecap=style.get("stroke-linecap", "butt"),
                fill_rule=style.get("fill-rule", "nonzero"),
                opacity=_f(style.get("opacity"), 1.0),
            )
            if paint.fill is None and paint.stroke is None:
                return
            # lines and open polylines have no area to fill
            if tag in ("line", "polyline"):
                paint.fill = None
                if paint.stroke is None:
                    return
            add(transform_segments(segs, m), paint)
        for child in el:
            walk(child, m, style)

    walk(root, IDENTITY, {})
    if not order:
        raise SvgError("SVG has no paths, shapes or lines that can be converted")
    return ParsedSvg([groups[k] for k in order], width, height, min_x, min_y, notes)


def fit(parsed: ParsedSvg, x: float, y: float, width: Optional[float],
        height: Optional[float]) -> Tuple[List[PathGroup], float, float, float]:
    """Scale/translate a parsed drawing into a ``width`` x ``height`` box at
    ``(x, y)``, keeping its aspect ratio. Returns ``(groups, w, h, scale)``.
    """
    aspect = parsed.width / parsed.height
    if width and height:
        scale = min(width / parsed.width, height / parsed.height)
    elif width:
        scale = width / parsed.width
    elif height:
        scale = height / parsed.height
    else:
        scale = 1.0
    w, h = parsed.width * scale, parsed.height * scale
    del aspect
    m: Matrix = (scale, 0, 0, scale, x - parsed.min_x * scale, y - parsed.min_y * scale)
    out = []
    for g in parsed.groups:
        paint = Paint(**{**g.paint.__dict__, "stroke_width": g.paint.stroke_width * scale})
        out.append(PathGroup(paint, transform_segments(g.segments, m)))
    return out, w, h, scale


def penpot_content(segs: Sequence[Seg]) -> List[dict]:
    """Segments -> the ``content`` vector Penpot stores on a path shape."""
    out: List[dict] = []
    for cmd, p in segs:
        if cmd == "M":
            out.append({"command": "move-to", "params": {"x": p[0], "y": p[1]}})
        elif cmd == "L":
            out.append({"command": "line-to", "params": {"x": p[0], "y": p[1]}})
        elif cmd == "C":
            out.append({"command": "curve-to", "params": {
                "c1x": p[0], "c1y": p[1], "c2x": p[2], "c2y": p[3], "x": p[4], "y": p[5]}})
        else:
            out.append({"command": "close-path", "params": {}})
    return out
