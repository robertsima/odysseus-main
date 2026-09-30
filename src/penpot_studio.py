"""Penpot design operations the stock Penpot MCP tools cannot do.

Why this exists (2026-09-30 diagnostics, Penpot Product Designer): the stdio
Penpot MCP offers rectangles, circles, text and frames, each written to the
page's ROOT frame. A designer agent therefore could not nest anything in a
board (layouts overlapped), could not place a real logo (it stacked circles),
could not see what it had made (no render), and its text boxes carried
``length * 0.6 * size`` bounds that never matched the drawn text.

This module talks to Penpot's HTTP API with the same access token the Penpot
MCP server uses and adds:

* a declarative builder: one call writes a whole nested tree of boards, shapes,
  text and vector icons, positions relative to the parent;
* SVG import (:mod:`src.penpot_svg`) and an icon-library search (Iconify:
  200k+ open-licensed icons, including game-icons' helmets and swords);
* a layout linter that reads a page back and reports overflow and overlap;
* a real render of a board through Penpot's own viewer.

Network and file changes live here; the MCP server in
``mcp_servers/penpot_studio_server.py`` is a thin wrapper.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from src import penpot_svg, penpot_text

ROOT_ID = "00000000-0000-0000-0000-000000000000"
ICONIFY_API = os.environ.get("ODYSSEUS_ICONIFY_API", "https://api.iconify.design").rstrip("/")
MAX_NODES = 600
MAX_DEPTH = 12


class PenpotError(RuntimeError):
    """A failure whose message tells the agent what to do next."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@dataclass
class PenpotConfig:
    base_url: str
    token: str
    source: str

    @property
    def public_url(self) -> str:
        return os.environ.get("PENPOT_PUBLIC_URL", "").strip().rstrip("/") or self.base_url


def _clean_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    return url


def _saved_penpot_env() -> Optional[Tuple[Dict[str, str], str]]:
    """The env of the Penpot MCP server saved under Settings > MCP, if any.

    The Penpot designer already has a working URL and access token there; the
    studio tools reuse them so nothing is configured twice.
    """
    try:
        from core.database import McpServer, SessionLocal
    except Exception:  # pragma: no cover - only when the app DB is unavailable
        return None
    db = SessionLocal()
    try:
        rows = db.query(McpServer).filter(McpServer.is_enabled == True).all()  # noqa: E712
        for row in rows:
            haystack = " ".join(str(v or "") for v in (row.name, row.command, row.args)).lower()
            if "penpot" not in haystack:
                continue
            try:
                env = json.loads(row.env) if row.env else {}
            except ValueError:
                continue
            if isinstance(env, dict) and env.get("PENPOT_ACCESS_TOKEN") and (
                    env.get("PENPOT_API_URL") or env.get("PENPOT_BASE_URL")):
                return {k: str(v) for k, v in env.items()}, f"MCP server {row.name!r}"
    except Exception:  # pragma: no cover
        return None
    finally:
        db.close()
    return None


def load_config() -> PenpotConfig:
    url = os.environ.get("PENPOT_API_URL") or os.environ.get("PENPOT_BASE_URL") or ""
    token = os.environ.get("PENPOT_ACCESS_TOKEN") or ""
    source = "environment"
    if not (url and token):
        saved = _saved_penpot_env()
        if saved:
            env, source = saved
            url = env.get("PENPOT_API_URL") or env.get("PENPOT_BASE_URL") or ""
            token = env.get("PENPOT_ACCESS_TOKEN") or ""
    if not (url and token):
        raise PenpotError(
            "Penpot is not configured for the studio tools. Add the Penpot MCP server under "
            "Settings > MCP with PENPOT_API_URL and PENPOT_ACCESS_TOKEN, or set both in the "
            "container environment.")
    return PenpotConfig(_clean_base(url), token, source)


class PenpotClient:
    """Minimal async client for Penpot's ``/api/rpc/command`` interface."""

    def __init__(self, cfg: PenpotConfig, timeout: float = 45.0):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=timeout, headers={
            "Authorization": f"Token {cfg.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    async def aclose(self) -> None:
        await self._http.aclose()

    async def rpc(self, command: str, body: Optional[dict] = None) -> Any:
        url = f"{self.cfg.base_url}/api/rpc/command/{command}"
        try:
            resp = await self._http.post(url, content=json.dumps(body or {}))
        except httpx.HTTPError as exc:
            raise PenpotError(
                f"cannot reach Penpot at {self.cfg.base_url} ({type(exc).__name__}: {exc}). "
                "Inside the Odysseus container this must be the NAS LAN address, not localhost.") from exc
        if resp.status_code == 401:
            raise PenpotError("Penpot rejected the access token (401). Make a new one under Penpot > "
                              "Your account > Access tokens and update the Penpot MCP server.")
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = resp.text[:400]
            raise PenpotError(f"Penpot {command} failed ({resp.status_code}): {json.dumps(err)[:700]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get_file(self, file_id: str) -> dict:
        return await self.rpc("get-file", {"id": file_id})

    async def apply(self, file_id: str, changes: List[dict], retries: int = 3) -> dict:
        """update-file with the current revision, retrying if someone else saved."""
        last: Optional[PenpotError] = None
        for _ in range(max(1, retries)):
            file = await self.get_file(file_id)
            try:
                return await self.rpc("update-file", {
                    "id": file_id,
                    "session-id": str(uuid.uuid4()),
                    "revn": file.get("revn", 0),
                    "vern": file.get("vern", 0),
                    "changes": changes,
                })
            except PenpotError as exc:
                last = exc
                if "revn" not in str(exc).lower() and "conflict" not in str(exc).lower():
                    raise
                await asyncio.sleep(0.4)
        raise last or PenpotError("update-file failed")


# ---------------------------------------------------------------------------
# Shape construction (Penpot JSON uses kebab-case keys on the wire)
# ---------------------------------------------------------------------------

def _rect_geom(x: float, y: float, w: float, h: float) -> dict:
    return {
        "x": x, "y": y, "width": w, "height": h, "rotation": 0,
        "selrect": {"x": x, "y": y, "width": w, "height": h,
                    "x1": x, "y1": y, "x2": x + w, "y2": y + h},
        "points": [{"x": x, "y": y}, {"x": x + w, "y": y},
                   {"x": x + w, "y": y + h}, {"x": x, "y": y + h}],
        "transform": {"a": 1.0, "b": 0.0, "c": 0.0, "d": 1.0, "e": 0.0, "f": 0.0},
        "transform-inverse": {"a": 1.0, "b": 0.0, "c": 0.0, "d": 1.0, "e": 0.0, "f": 0.0},
    }


_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def norm_color(value: Any, default: Optional[str] = None) -> Optional[str]:
    """``#abc`` / ``#aabbcc`` / ``None`` -> ``#aabbcc`` (Penpot wants 6 digits)."""
    if value in (None, "", "none", "transparent"):
        return default
    text = str(value).strip()
    if not _HEX.match(text):
        raise PenpotError(f"colour {value!r} is not a #rgb or #rrggbb hex value")
    if len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.lower()


def _fills(color: Any, opacity: float = 1.0) -> List[dict]:
    c = norm_color(color)
    return [{"fill-color": c, "fill-opacity": opacity}] if c else []


def _strokes(spec: Any, default_align: str = "inner") -> List[dict]:
    if not spec:
        return []
    if isinstance(spec, str):
        spec = {"color": spec}
    color = norm_color(spec.get("color"), "#000000")
    return [{
        "stroke-color": color,
        "stroke-opacity": float(spec.get("opacity", 1)),
        "stroke-width": float(spec.get("width", 2)),
        "stroke-style": spec.get("style", "solid"),
        "stroke-alignment": spec.get("align", default_align),
    }]


def _shadows(spec: Any) -> List[dict]:
    if not spec:
        return []
    items = spec if isinstance(spec, list) else [spec]
    out = []
    for item in items:
        out.append({
            "id": str(uuid.uuid4()),
            "style": item.get("style", "drop-shadow"),
            "offset-x": float(item.get("x", 6)),
            "offset-y": float(item.get("y", 6)),
            "blur": float(item.get("blur", 0)),
            "spread": float(item.get("spread", 0)),
            "hidden": False,
            "color": {"color": norm_color(item.get("color"), "#000000"),
                      "opacity": float(item.get("opacity", 1))},
        })
    return out


def _num(node: dict, *names: str, default: Optional[float] = None) -> Optional[float]:
    for name in names:
        if node.get(name) is not None:
            try:
                return float(node[name])
            except (TypeError, ValueError):
                raise PenpotError(f"{name!r} must be a number, got {node[name]!r}")
    return default


FONT_SLUG = re.compile(r"[^a-z0-9]+")


def font_fields(family: str, weight: str, italic: bool) -> dict:
    """Penpot's Google-font identifiers: ``gfont-work-sans`` + variant ``700``."""
    weight = str(weight)
    if weight in ("normal", "regular"):
        weight = "400"
    elif weight == "bold":
        weight = "700"
    if weight == "400":
        variant = "italic" if italic else "regular"
    else:
        variant = f"{weight}italic" if italic else weight
    return {
        "font-id": "gfont-" + FONT_SLUG.sub("-", family.lower()).strip("-"),
        "font-family": family,
        "font-variant-id": variant,
        "font-weight": weight,
        "font-style": "italic" if italic else "normal",
    }


def text_spec(node: dict) -> penpot_text.TextSpec:
    """The measurable description of a text node (also used to build it)."""
    text = str(node.get("text", ""))
    if not text:
        raise PenpotError("text node has no 'text'")
    size = _num(node, "size", "font_size", default=16) or 16
    shown = text.upper() if node.get("uppercase") else text
    weight = str(node.get("weight", "400"))
    weight = {"normal": "400", "regular": "400", "bold": "700"}.get(weight, weight)
    return penpot_text.TextSpec(
        text=shown, family=str(node.get("family", "Work Sans")), weight=weight,
        italic=bool(node.get("italic")), size=float(size),
        letter_spacing=float(node.get("letter_spacing", 0) or 0),
        max_width=_num(node, "w", "width"))


def _collect_text_nodes(nodes: Iterable[Any], out: List[dict], depth: int = 0) -> None:
    for node in nodes or []:
        if not isinstance(node, dict) or depth > MAX_DEPTH:
            continue
        if str(node.get("type", "")).lower() == "text":
            out.append(node)
        _collect_text_nodes(node.get("children"), out, depth + 1)


class _Builder:
    """Turns a declarative node tree into ``add-obj`` changes."""

    def __init__(self, page_id: str, parent: dict, origin: Tuple[float, float],
                 measured: Optional[Dict[int, penpot_text.Measured]] = None):
        self.measured = measured or {}
        self.inexact_text = 0
        self.page_id = page_id
        self.changes: List[dict] = []
        self.created: List[dict] = []
        self.origin = origin
        self.parent = parent  # {"id", "frame", "is_frame"}
        self.count = 0
        self.attributions: List[str] = []
        self.notes: List[str] = []

    # -- helpers ----------------------------------------------------------
    def _add(self, obj: dict, parent: dict, frame_id: str) -> None:
        obj["parent-id"] = parent["id"]
        obj["frame-id"] = frame_id
        self.changes.append({
            "type": "add-obj", "id": obj["id"], "page-id": self.page_id,
            "parent-id": parent["id"], "frame-id": frame_id, "obj": obj,
        })
        self.created.append({
            "id": obj["id"], "name": obj.get("name"), "type": obj["type"],
            "x": round(obj["x"], 1), "y": round(obj["y"], 1),
            "width": round(obj["width"], 1), "height": round(obj["height"], 1),
            "parent": parent["id"],
        })

    def _common(self, node: dict, kind: str, default_name: str, x: float, y: float,
                w: float, h: float) -> dict:
        obj = {"id": str(uuid.uuid4()), "type": kind, "name": str(node.get("name") or default_name),
               **_rect_geom(x, y, w, h)}
        if node.get("opacity") is not None:
            obj["opacity"] = float(node["opacity"])
        shadow = _shadows(node.get("shadow"))
        if shadow:
            obj["shadow"] = shadow
        if node.get("blur"):
            obj["blur"] = {"id": str(uuid.uuid4()), "type": "layer-blur",
                           "value": float(node["blur"]), "hidden": False}
        return obj

    # -- node types -------------------------------------------------------
    async def build(self, node: Any, parent: dict, ox: float, oy: float, depth: int = 0) -> Optional[dict]:
        if not isinstance(node, dict):
            raise PenpotError("each node must be an object with a 'type'")
        if depth > MAX_DEPTH:
            raise PenpotError(f"nesting deeper than {MAX_DEPTH} levels")
        self.count += 1
        if self.count > MAX_NODES:
            raise PenpotError(f"more than {MAX_NODES} nodes in one build; split it into several calls")
        kind = str(node.get("type", "")).lower()
        x = ox + (_num(node, "x", default=0) or 0)
        y = oy + (_num(node, "y", default=0) or 0)
        frame_id = parent["id"] if parent["is_frame"] else parent["frame"]
        handler = {
            "frame": self._frame, "board": self._frame, "rect": self._rect, "rectangle": self._rect,
            "ellipse": self._ellipse, "circle": self._ellipse, "text": self._text,
            "icon": self._svg, "svg": self._svg, "path": self._path, "group": self._group,
        }.get(kind)
        if handler is None:
            raise PenpotError(f"unknown node type {kind!r}; use frame, rect, ellipse, text, icon, svg, path or group")
        return await handler(node, parent, frame_id, x, y, depth)

    async def _frame(self, node, parent, frame_id, x, y, depth):
        w = _num(node, "w", "width")
        h = _num(node, "h", "height")
        if not w or not h:
            raise PenpotError(f"frame {node.get('name')!r} needs w and h")
        obj = self._common(node, "frame", "Board", x, y, w, h)
        obj.update({
            "fills": _fills(node.get("fill", "#ffffff"), float(node.get("fill_opacity", 1))),
            "strokes": _strokes(node.get("stroke")),
            "shapes": [], "show-content": True, "hide-in-viewer": False,
            "clip-content": bool(node.get("clip", True)),
        })
        r = _num(node, "radius", default=0) or 0
        obj.update({"r1": r, "r2": r, "r3": r, "r4": r})
        is_board = parent["id"] == ROOT_ID
        if is_board:
            frame_id = ROOT_ID
        self._add(obj, parent, frame_id)
        me = {"id": obj["id"], "frame": obj["id"], "is_frame": True}
        for child in node.get("children") or []:
            await self.build(child, me, x, y, depth + 1)
        return obj

    async def _rect(self, node, parent, frame_id, x, y, depth):
        w = _num(node, "w", "width")
        h = _num(node, "h", "height")
        if not w or not h:
            raise PenpotError(f"rect {node.get('name')!r} needs w and h")
        obj = self._common(node, "rect", "Rectangle", x, y, w, h)
        obj["fills"] = _fills(node.get("fill"), float(node.get("fill_opacity", 1)))
        obj["strokes"] = _strokes(node.get("stroke"))
        r = _num(node, "radius", default=0) or 0
        obj.update({"r1": r, "r2": r, "r3": r, "r4": r})
        self._add(obj, parent, frame_id)
        return obj

    async def _ellipse(self, node, parent, frame_id, x, y, depth):
        w = _num(node, "w", "width", "size")
        h = _num(node, "h", "height", "size")
        if not w or not h:
            raise PenpotError(f"ellipse {node.get('name')!r} needs w and h (or size)")
        obj = self._common(node, "circle", "Ellipse", x, y, w, h)
        obj["fills"] = _fills(node.get("fill"), float(node.get("fill_opacity", 1)))
        obj["strokes"] = _strokes(node.get("stroke"))
        self._add(obj, parent, frame_id)
        return obj

    async def _text(self, node, parent, frame_id, x, y, depth):
        spec = text_spec(node)
        size = spec.size
        lh = _num(node, "line_height", default=1.2) or 1.2
        m = self.measured.get(id(node)) or penpot_text.estimate(spec)
        if not m.exact:
            self.inexact_text += 1
        line_px = size * lh
        fixed_w = spec.max_width
        box_w = fixed_w or max(line.width for line in m.lines) or size
        box_h = len(m.lines) * line_px
        color = norm_color(node.get("color"), "#000000")
        fonts = font_fields(spec.family, spec.weight, spec.italic)
        transform = "uppercase" if node.get("uppercase") else "none"
        align = str(node.get("align", "left"))
        size_s = str(int(size) if size == int(size) else size)
        style = {
            **fonts, "font-size": size_s, "text-decoration": "none", "text-transform": transform,
            "letter-spacing": str(spec.letter_spacing), "line-height": str(lh),
            "fills": [{"fill-color": color, "fill-opacity": 1}],
        }
        paragraphs = []
        for line in str(node.get("text", "")).split("\n"):
            paragraphs.append({"type": "paragraph", "text-align": align, **style,
                               "children": [{**style, "text": line}]})
        # one record per DRAWN line; y is the bottom of the line box
        position = []
        for i, line in enumerate(m.lines):
            lx = x + {"center": (box_w - line.width) / 2, "right": box_w - line.width}.get(align, 0.0)
            position.append({
                "x": round(lx, 2), "y": round(y + (i + 1) * line_px, 2),
                "width": round(line.width, 2), "height": round(line_px, 2),
                "text": line.text, **style, "direction": "ltr",
            })
        obj = self._common(node, "text", str(node.get("name") or str(node["text"]).split("\n")[0][:40]),
                           x, y, box_w, box_h)
        obj.update({
            "content": {"type": "root", "children": [{"type": "paragraph-set", "children": paragraphs}]},
            "grow-type": "auto-height" if fixed_w else "auto-width",
            "vertical-align": str(node.get("valign", "top")),
            "position-data": position, "fills": [],
        })
        self._add(obj, parent, frame_id)
        return obj

    async def _path(self, node, parent, frame_id, x, y, depth):
        d = node.get("d")
        if not d:
            raise PenpotError("path node needs 'd'")
        w, h = _num(node, "w", "width"), _num(node, "h", "height")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{node.get("viewbox", "0 0 24 24")}">'
               f'<path d="{d}"/></svg>')
        return await self._place_svg(svg, node, parent, frame_id, x, y, w, h)

    async def _svg(self, node, parent, frame_id, x, y, depth):
        markup = node.get("svg")
        icon = node.get("icon")
        size = _num(node, "size")
        w = _num(node, "w", "width", default=size)
        h = _num(node, "h", "height", default=size)
        if not markup:
            if icon:
                markup, credit = await fetch_icon(str(icon))
                if credit:
                    self.attributions.append(credit)
            elif node.get("url"):
                markup = await fetch_svg_url(str(node["url"]))
            else:
                raise PenpotError("an icon/svg node needs 'icon' (e.g. 'game-icons:spartan-helmet'), "
                                  "'svg' markup or 'url'")
        return await self._place_svg(markup, node, parent, frame_id, x, y, w, h)

    async def _place_svg(self, markup, node, parent, frame_id, x, y, w, h):
        parsed = penpot_svg.parse_svg(markup)
        for note in parsed.notes:
            if note not in self.notes:
                self.notes.append(note)
        if not w and not h:
            w = 24.0 if max(parsed.width, parsed.height) <= 64 else min(parsed.width, 240.0)
        groups, tw, th, _scale = penpot_svg.fit(parsed, x, y, w, h)
        color = norm_color(node.get("color"))
        stroke_color = norm_color(node.get("stroke_color")) or color
        name = str(node.get("name") or node.get("icon") or "Vector")
        shapes: List[dict] = []
        holder = parent
        group_obj = None
        if len(groups) > 1:
            group_obj = self._common(node, "group", name, x, y, tw, th)
            group_obj["shapes"] = []
            self._add(group_obj, parent, frame_id)
            holder = {"id": group_obj["id"], "frame": frame_id, "is_frame": False}
        for index, g in enumerate(groups):
            x0, y0, x1, y1 = penpot_svg.segments_bbox(g.segments)
            sw = g.paint.stroke_width if g.paint.stroke else 0.0
            obj = self._common({} if group_obj else node, "path",
                               name if not group_obj else f"{name} {index + 1}",
                               x0, y0, max(x1 - x0, 0.01), max(y1 - y0, 0.01))
            obj["content"] = penpot_svg.penpot_content(g.segments)
            fill = g.paint.fill
            if fill == "currentColor":
                fill = color or "#000000"
            elif color and node.get("recolor"):
                fill = color
            fills = _fills(fill, g.paint.fill_opacity * g.paint.opacity) if fill else []
            obj["fills"] = fills
            if g.paint.stroke:
                s = g.paint.stroke
                if s == "currentColor":
                    s = stroke_color or "#000000"
                elif stroke_color and node.get("recolor"):
                    s = stroke_color
                caps = {"round": "round", "square": "square"}.get(g.paint.linecap)
                stroke = {"stroke-color": norm_color(s, "#000000"),
                          "stroke-opacity": g.paint.stroke_opacity,
                          "stroke-width": round(sw, 3), "stroke-style": "solid",
                          "stroke-alignment": "center"}
                if caps:
                    stroke["stroke-cap-start"] = caps
                    stroke["stroke-cap-end"] = caps
                obj["strokes"] = [stroke]
            if g.paint.fill_rule == "evenodd":
                obj["svg-attrs"] = {"fill-rule": "evenodd"}
            self._add(obj, holder, frame_id)
            shapes.append(obj)
        return group_obj or shapes[0]

    async def _group(self, node, parent, frame_id, x, y, depth):
        children = node.get("children") or []
        if not children:
            raise PenpotError("group needs children")
        start = len(self.created)
        obj = self._common(node, "group", "Group", x, y, 1, 1)
        obj["shapes"] = []
        self._add(obj, parent, frame_id)
        me = {"id": obj["id"], "frame": frame_id, "is_frame": False}
        for child in children:
            await self.build(child, me, x, y, depth + 1)
        kids = [c for c in self.created[start + 1:] if c["parent"] == obj["id"]]
        if kids:
            gx = min(k["x"] for k in kids)
            gy = min(k["y"] for k in kids)
            gx2 = max(k["x"] + k["width"] for k in kids)
            gy2 = max(k["y"] + k["height"] for k in kids)
            obj.update(_rect_geom(gx, gy, gx2 - gx, gy2 - gy))
            self.created[start].update(x=round(gx, 1), y=round(gy, 1),
                                       width=round(gx2 - gx, 1), height=round(gy2 - gy, 1))
        return obj


def _find_parent(file: dict, page_id: str, parent_id: Optional[str]) -> Tuple[dict, Tuple[float, float]]:
    pages = (file.get("data") or {}).get("pagesIndex") or {}
    page = pages.get(page_id)
    if page is None:
        raise PenpotError(f"page {page_id} is not in this file. Pages: " +
                          ", ".join(f"{pid} ({p.get('name')})" for pid, p in pages.items()))
    if not parent_id or parent_id == ROOT_ID:
        return {"id": ROOT_ID, "frame": ROOT_ID, "is_frame": True}, (0.0, 0.0)
    obj = (page.get("objects") or {}).get(parent_id)
    if obj is None:
        raise PenpotError(f"parent shape {parent_id} is not on page {page_id}")
    kind = obj.get("type")
    if kind not in ("frame", "group"):
        raise PenpotError(f"parent shape {parent_id} is a {kind}; children go inside a frame or group")
    frame = obj["id"] if kind == "frame" else obj.get("frameId", ROOT_ID)
    return {"id": obj["id"], "frame": frame, "is_frame": kind == "frame"}, (
        float(obj.get("x", 0)), float(obj.get("y", 0)))


async def build_tree(client: PenpotClient, file_id: str, page_id: str,
                     nodes: List[dict], parent_id: Optional[str] = None,
                     dry_run: bool = False) -> dict:
    """Add ``nodes`` (positions relative to the parent) in one atomic update."""
    if not isinstance(nodes, list) or not nodes:
        raise PenpotError("'nodes' must be a non-empty list")
    file = await client.get_file(file_id)
    parent, origin = _find_parent(file, page_id, parent_id)
    texts: List[dict] = []
    _collect_text_nodes(nodes, texts)
    measured: Dict[int, penpot_text.Measured] = {}
    if texts:
        for node, m in zip(texts, await penpot_text.measure([text_spec(n) for n in texts])):
            measured[id(node)] = m
    builder = _Builder(page_id, parent, origin, measured)
    for node in nodes:
        await builder.build(node, parent, origin[0], origin[1])
    if not dry_run:
        await client.apply(file_id, builder.changes)
    return {
        "created": builder.created, "count": len(builder.created),
        "attributions": sorted(set(builder.attributions)), "applied": not dry_run,
        "text_measured_in_browser": bool(texts) and builder.inexact_text == 0,
        "warnings": ([f"{builder.inexact_text} text node(s) were sized by estimate (no browser or font "
                      "available to measure); they may look stretched or squeezed until opened in Penpot"]
                     if builder.inexact_text else []) + [f"svg: {n}" for n in builder.notes],
    }


async def move_shapes(client: PenpotClient, file_id: str, page_id: str,
                      shape_ids: List[str], parent_id: str) -> dict:
    """Re-parent existing shapes (what the stock tools cannot: they always
    write to the root frame). Absolute positions are kept."""
    file = await client.get_file(file_id)
    parent, _ = _find_parent(file, page_id, parent_id)
    if parent["id"] == ROOT_ID:
        raise PenpotError("moving shapes to the page root is not supported here")
    objects = file["data"]["pagesIndex"][page_id]["objects"]
    missing = [s for s in shape_ids if s not in objects]
    if missing:
        raise PenpotError(f"shapes not on page {page_id}: {', '.join(missing)}")
    changes = [{
        "type": "mov-objects", "page-id": page_id, "parent-id": parent["id"],
        "frame-id": parent["id"] if parent["is_frame"] else parent["frame"],
        "shapes": shape_ids,
    }]
    await client.apply(file_id, changes)
    return {"moved": shape_ids, "into": parent["id"]}


# ---------------------------------------------------------------------------
# Icon library (Iconify)
# ---------------------------------------------------------------------------

async def _get(url: str, *, params: Optional[dict] = None, timeout: float = 20.0) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": "Odysseus-PenpotStudio/1.0"}) as http:
            resp = await http.get(url, params=params)
    except httpx.HTTPError as exc:
        raise PenpotError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise PenpotError(f"{url} answered HTTP {resp.status_code}")
    return resp


async def search_icons(query: str, prefix: Optional[str] = None, limit: int = 24) -> dict:
    """Search open icon sets. Returns ids like ``game-icons:spartan-helmet`` plus
    each set's licence so the designer can attribute correctly."""
    params: Dict[str, Any] = {"query": query, "limit": max(1, min(int(limit), 64))}
    if prefix:
        params["prefix"] = prefix
    data = (await _get(f"{ICONIFY_API}/search", params=params)).json()
    collections = data.get("collections") or {}
    sets = {}
    for pfx, info in collections.items():
        lic = info.get("license") or {}
        author = info.get("author") or {}
        sets[pfx] = {
            "name": info.get("name"), "author": author.get("name"),
            "license": lic.get("title") or lic.get("spdx"),
            "attribution_required": _needs_attribution(lic),
            "monotone": not info.get("palette", False),
        }
    return {"icons": data.get("icons", []), "total": data.get("total", 0), "sets": sets}


def _needs_attribution(lic: dict) -> bool:
    spdx = str(lic.get("spdx") or lic.get("title") or "").lower()
    return not any(tag in spdx for tag in ("mit", "isc", "0bsd", "cc0", "unlicense", "public"))


async def fetch_icon(icon: str) -> Tuple[str, Optional[str]]:
    """``prefix:name`` -> (svg markup, attribution line or None)."""
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]*):([a-z0-9][a-z0-9-]*)", icon.strip().lower())
    if not m:
        raise PenpotError(f"icon {icon!r} must look like 'game-icons:spartan-helmet' "
                          "(use penpot_search_icons to find ids)")
    prefix, name = m.groups()
    svg = (await _get(f"{ICONIFY_API}/{prefix}/{name}.svg")).text
    if "<svg" not in svg:
        raise PenpotError(f"icon {icon} does not exist (Iconify returned no drawing)")
    credit = None
    try:
        info = (await _get(f"{ICONIFY_API}/collections", params={"prefixes": prefix})).json().get(prefix) or {}
        lic = info.get("license") or {}
        if _needs_attribution(lic):
            author = (info.get("author") or {}).get("name", "the icon set's authors")
            credit = f"{info.get('name', prefix)} by {author} ({lic.get('title') or lic.get('spdx')}) via iconify.design"
    except PenpotError:
        credit = f"{prefix} icon set via iconify.design (check its licence before shipping)"
    return svg, credit


def _ip_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


async def _resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _assert_public(url: str) -> None:
    """Refuse a URL unless every address its host resolves to is public."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise PenpotError("svg url must be http(s)")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise PenpotError("svg url points at a private address; only public URLs are fetched")
    try:
        addrs = await _resolve(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise PenpotError(f"cannot resolve {host}: {exc}") from exc
    if not addrs or not all(_ip_is_public(a) for a in addrs):
        raise PenpotError("svg url points at a private address; only public URLs are fetched")


async def fetch_svg_url(url: str, max_bytes: int = 2_000_000) -> str:
    """Fetch a public SVG. Redirects are followed by hand so each hop is
    re-validated (a public URL must not bounce us to an internal one)."""
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False,
                                 headers={"User-Agent": "Odysseus-PenpotStudio/1.0"}) as http:
        for _ in range(5):
            await _assert_public(url)
            try:
                resp = await http.get(url)
            except httpx.HTTPError as exc:
                raise PenpotError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
            if resp.is_redirect and resp.headers.get("location"):
                url = str(resp.url.join(resp.headers["location"]))
                continue
            break
        else:
            raise PenpotError("too many redirects fetching the svg url")
    if resp.status_code >= 400:
        raise PenpotError(f"{url} answered HTTP {resp.status_code}")
    if len(resp.content) > max_bytes:
        raise PenpotError("that SVG is too large to import")
    text = resp.text
    if "<svg" not in text.lower():
        raise PenpotError("that URL did not return SVG markup (an HTML page, not the raw .svg file?)")
    return text


# ---------------------------------------------------------------------------
# Reading back: layout lint
# ---------------------------------------------------------------------------

def _box(obj: dict) -> Tuple[float, float, float, float]:
    sel = obj.get("selrect") or {}
    x = float(sel.get("x", obj.get("x", 0)))
    y = float(sel.get("y", obj.get("y", 0)))
    return x, y, float(sel.get("width", obj.get("width", 0))), float(sel.get("height", obj.get("height", 0)))


def _inter(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _text_leaves(node: Any) -> Iterable[Tuple[str, dict]]:
    """``(text, style)`` for every text run in a Penpot text ``content`` tree."""
    if isinstance(node, dict):
        if "text" in node and not node.get("children"):
            yield str(node.get("text") or ""), node
        for child in node.get("children") or []:
            yield from _text_leaves(child)


def describe_page(file: dict, page_id: str, frame_id: Optional[str] = None,
                  tolerance: float = 1.0) -> dict:
    """Tree outline plus layout problems for a page (or one board).

    Problems: a child that sticks out of its board (clipped by the board), and
    two content shapes in the same board that partly overlap (neither contains
    the other, so it is not just a label sitting on a card). Text bounds are
    Penpot's stored ones: approximate until the file has been opened in Penpot.
    """
    pages = (file.get("data") or {}).get("pagesIndex") or {}
    page = pages.get(page_id)
    if page is None:
        raise PenpotError(f"page {page_id} is not in this file")
    objects: Dict[str, dict] = page.get("objects") or {}
    start = frame_id or ROOT_ID
    if start not in objects:
        raise PenpotError(f"shape {start} is not on page {page_id}")

    outline: List[dict] = []
    problems: List[str] = []
    palette: Dict[str, int] = {}
    fonts: set = set()

    def label(o: dict) -> str:
        return f"{o.get('type')} '{o.get('name')}' ({o.get('id')})"

    def walk(oid: str, depth: int) -> None:
        o = objects[oid]
        x, y, w, h = _box(o)
        if oid != ROOT_ID:
            entry = {"id": oid, "type": o.get("type"), "name": o.get("name"),
                     "depth": depth, "x": round(x, 1), "y": round(y, 1),
                     "w": round(w, 1), "h": round(h, 1)}
            fill = next((f.get("fillColor") for f in o.get("fills") or [] if f.get("fillColor")), None)
            if fill:
                entry["fill"] = fill
                palette[fill] = palette.get(fill, 0) + 1
            stroke = next(((s.get("strokeColor"), s.get("strokeWidth"))
                           for s in o.get("strokes") or [] if s.get("strokeColor")), None)
            if stroke:
                entry["stroke"] = f"{stroke[0]} {stroke[1]}px"
            if o.get("type") == "text":
                leaves = list(_text_leaves(o.get("content")))
                entry["text"] = " / ".join(t for t, _ in leaves)[:200]
                if leaves:
                    lf = leaves[0][1]
                    entry["font"] = f"{lf.get('fontFamily')} {lf.get('fontWeight')} {lf.get('fontSize')}px"
                    fonts.add(str(lf.get("fontFamily")))
                    color = next((f.get("fillColor") for f in lf.get("fills") or []), None)
                    if color:
                        entry["color"] = color
                        palette[color] = palette.get(color, 0) + 1
                if not o.get("positionData"):
                    problems.append(f"NO-RENDER: text {label(o)} has no position-data, so Penpot's "
                                    "viewer and exporter draw nothing for it")
            outline.append(entry)
        kids = [k for k in (o.get("shapes") or []) if k in objects]
        if o.get("type") == "frame" and oid != ROOT_ID:
            boxes = {k: _box(objects[k]) for k in kids}
            for k, (kx, ky, kw, kh) in boxes.items():
                if kx < x - tolerance or ky < y - tolerance or kx + kw > x + w + tolerance or ky + kh > y + h + tolerance:
                    problems.append(f"OVERFLOW: {label(objects[k])} extends outside board '{o.get('name')}' "
                                    f"(child {round(kx)},{round(ky)} {round(kw)}x{round(kh)}; board "
                                    f"{round(x)},{round(y)} {round(w)}x{round(h)})")
            ids = list(boxes)
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    ba, bb = boxes[a], boxes[b]
                    inter = _inter(ba, bb)
                    if inter <= 0:
                        continue
                    smaller = min(ba[2] * ba[3], bb[2] * bb[3]) or 1.0
                    if inter >= smaller - 1.0:  # one contains the other: a label on a card
                        continue
                    kinds = {objects[a].get("type"), objects[b].get("type")}
                    if "text" in kinds or inter / smaller > 0.15:
                        problems.append(f"OVERLAP: {label(objects[a])} and {label(objects[b])} overlap by "
                                        f"{round(100 * inter / smaller)}% of the smaller shape")
        for k in kids:
            walk(k, depth + 1)

    walk(start, 0)
    return {"shapes": outline, "problems": problems, "shape_count": len(outline),
            "palette": dict(sorted(palette.items(), key=lambda kv: -kv[1])[:12]),
            "fonts": sorted(fonts)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

async def share_link(client: PenpotClient, file_id: str, page_id: str) -> str:
    """Mint a view-only share id for one page; the caller deletes it."""
    link = await client.rpc("create-share-link", {
        "file-id": file_id, "who-comment": "team", "who-inspect": "team", "pages": [page_id]})
    sid = (link or {}).get("id")
    if not sid:
        raise PenpotError(f"Penpot returned no share link id: {link!r}")
    return sid


def viewer_url(cfg: PenpotConfig, file_id: str, page_id: str, frame_id: str, share_id: str) -> str:
    return (f"{cfg.public_url}/#/view?file-id={file_id}&page-id={page_id}"
            f"&section=interactions&frame-id={frame_id}&index=0&share-id={share_id}")


def find_boards(file: dict, page_id: str) -> List[dict]:
    page = ((file.get("data") or {}).get("pagesIndex") or {}).get(page_id) or {}
    objects = page.get("objects") or {}
    root = objects.get(ROOT_ID) or {}
    out = []
    for k in root.get("shapes") or []:
        o = objects.get(k) or {}
        if o.get("type") == "frame":
            x, y, w, h = _box(o)
            out.append({"id": k, "name": o.get("name"), "x": x, "y": y, "w": w, "h": h})
    return out


browser_executable = penpot_text.browser_executable


async def render_board(client: PenpotClient, file_id: str, page_id: str,
                       frame_id: Optional[str], out_png: str, *, width: Optional[int] = None,
                       height: Optional[int] = None, wait_ms: int = 12000) -> dict:
    """Screenshot a board in Penpot's own viewer with headless Chromium.

    The viewer renders with Penpot's real engine and fonts, so this shows what
    the user will see. It needs only the access token (to mint a view-only
    share link) and a Chromium on the same network as Penpot.
    """
    file = await client.get_file(file_id)
    boards = find_boards(file, page_id)
    if not boards:
        raise PenpotError(f"page {page_id} has no boards (top-level frames) to render; "
                          "put the design inside a board")
    board = next((b for b in boards if b["id"] == frame_id), None) if frame_id else boards[0]
    if board is None:
        raise PenpotError(f"{frame_id} is not a top-level board on that page. Boards: " +
                          ", ".join(f"{b['id']} ({b['name']})" for b in boards))
    sid = await share_link(client, file_id, page_id)
    url = viewer_url(client.cfg, file_id, page_id, board["id"], sid)
    keep_link = False
    try:
        w = int(width or min(max(board["w"] + 80, 480), 2400))
        h = int(height or min(max(board["h"] + 120, 360), 2400))
        exe = browser_executable()
        if not exe:
            keep_link = True  # the agent is being handed this link to open itself
            raise PenpotError("no Chromium/Chrome found for rendering. Open this view-only link in the "
                              f"browser tool and take a screenshot instead: {url}")
        if os.path.exists(out_png):
            os.remove(out_png)
        with tempfile.TemporaryDirectory(prefix="penpot-render-profile-") as profile:
            args = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                    "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={w},{h}",
                    f"--user-data-dir={profile}", f"--virtual-time-budget={wait_ms}",
                    f"--screenshot={out_png}", url]
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=wait_ms / 1000 + 45)
            except asyncio.TimeoutError:
                proc.kill()
                raise PenpotError("rendering timed out; Penpot's viewer did not finish loading")
        if not os.path.isfile(out_png) or os.path.getsize(out_png) < 1000:
            raise PenpotError("the browser produced no screenshot: " + (err or b"").decode("utf-8", "replace")[-300:])
    finally:
        if not keep_link:
            try:
                await client.rpc("delete-share-link", {"id": sid})
            except PenpotError:
                pass  # a stale view-only link to a LAN Penpot is low risk; nothing to do
    return {"path": out_png, "board": board, "viewer_url": url, "width": w, "height": h}
