"""
penpot_studio_server.py

Built-in MCP server that fills the gaps in the stock Penpot MCP tools: nested
layouts, real vector icons and logos, a readable design dump with a layout
linter, and a real render of a board so the designer can SEE its work.

It reuses the Penpot URL and access token saved for the Penpot MCP server (or
PENPOT_API_URL / PENPOT_ACCESS_TOKEN in the environment). The logic lives in
src/penpot_studio.py.
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import penpot_studio as ps  # noqa: E402
from src.penpot_svg import SvgError  # noqa: E402

server = Server("penpot_studio")

_NODE_HELP = (
    "Each node is an object with a 'type' and coordinates RELATIVE TO ITS PARENT (x, y from the parent's "
    "top-left). Types: "
    "frame {name,x,y,w,h,fill,radius,stroke,shadow,clip,children} (a top-level frame is a board); "
    "rect {name,x,y,w,h,fill,radius,stroke,shadow}; ellipse {x,y,w,h,fill,stroke}; "
    "text {text,x,y,size,weight,family,color,align,w,line_height,letter_spacing,uppercase,italic} "
    "(give w to wrap at a width, omit it for one line; weight '400'..'900'; family is any Google font, "
    "default 'Work Sans'; '\\n' breaks a line); "
    "icon {icon:'prefix:name',x,y,size,color} (a real vector from the icon library: find ids with search_icons); "
    "svg {svg:'<svg...>',x,y,w,h,color}; path {d,viewbox,x,y,w,h,fill}; group {children}. "
    "Colours are #rgb/#rrggbb. stroke is {color,width,align:'inner'|'center'|'outer'}; "
    "shadow is {x,y,blur,spread,color,opacity} (blur 0 gives the hard offset shadow of neo-brutalism). "
    "Add icon/svg 'recolor': true to force the colour over an svg's own fills."
)

_ID = {"type": "string", "description": "UUID"}

TOOLS = [
    Tool(
        name="search_icons",
        description=(
            "Search 200k open vector icons (game-icons has helmets, swords and soldiers; tabler, lucide, "
            "phosphor, material) and return ids like 'game-icons:spartan-helmet' with each set's licence; "
            "an id is a build_design icon node. For a logo, emblem or illustration in code, a page or a "
            "file, search here, then call again with ids=[...] (max 6) for each icon's standalone <svg> "
            "markup, licence, author, source URL and the attribution line to paste into ACKNOWLEDGMENTS. "
            "CC BY sets need that credit; build_design reports it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the icon shows, e.g. 'helmet', 'sword', 'shield'."},
                "set": {"type": "string", "description": "Optional icon-set prefix to search within, e.g. 'game-icons', 'tabler', 'lucide'."},
                "limit": {"type": "integer", "description": "Max results (default 24, max 64)."},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "Icon ids from a search (e.g. 'game-icons:spartan-helmet'). Returns their full SVG markup and licence/attribution instead of searching; the server requires `query` on every call, so repeat your search words. Max 6 per call."},
            },
            "required": ["query"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    ),
    Tool(
        name="build_design",
        description=(
            "Write a whole design into a Penpot page in ONE call: nested boards, rectangles, text, and real "
            "vector icons/SVG, all placed relative to their parent so nothing overlaps by accident. Use this "
            "instead of many create_rectangle/create_text calls (those always write to the page root and "
            "cannot nest, and their text does not render in previews). Text is measured in a browser with "
            "the real font, so it draws at the right size. Then call render_preview to look at the result "
            "and check_layout for overlap/overflow. Use dry_run to validate without writing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {**_ID, "description": "Penpot file UUID"},
                "page_id": {**_ID, "description": "Page UUID"},
                "nodes": {"type": "array", "items": {"type": "object"}, "description": _NODE_HELP},
                "parent_id": {"type": "string", "description": "Optional frame/group UUID to build inside; omit for the page root (top-level nodes become boards)."},
                "dry_run": {"type": "boolean", "description": "Validate and report without writing."},
            },
            "required": ["file_id", "page_id", "nodes"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    ),
    Tool(
        name="move_shapes",
        description=(
            "Move existing shapes into a frame or group (the stock create_* tools leave everything on the "
            "page root). Keeps their absolute position."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": _ID, "page_id": _ID,
                "shape_ids": {"type": "array", "items": {"type": "string"}},
                "parent_id": {**_ID, "description": "Destination frame or group UUID"},
            },
            "required": ["file_id", "page_id", "shape_ids", "parent_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    ),
    Tool(
        name="inspect_design",
        description=(
            "Read an existing design: pages, or one page/board as an outline with positions, sizes, fills, "
            "strokes, text content, fonts, plus the colour palette and font list, and a layout check that "
            "reports OVERFLOW (child sticks out of its board), OVERLAP (two shapes partly cover each other) "
            "and NO-RENDER (text that previews will not draw). Omit page_id to list the file's pages and boards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": _ID,
                "page_id": {**_ID, "description": "Page to read; omit to list pages."},
                "frame_id": {**_ID, "description": "Limit to one board/frame."},
            },
            "required": ["file_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    ),
    Tool(
        name="render_preview",
        description=(
            "See a board: renders a top-level frame with Penpot's own renderer and real fonts. Use it after "
            "building and after each fix, and look for overlap, clipping, low contrast and broken icons. "
            "An error means Penpot's viewer showed its error page; no error image is ever returned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": _ID,
                "page_id": _ID,
                "frame_id": {**_ID, "description": "Top-level board to render; default the first board on the page."},
                "width": {"type": "integer", "description": "Viewport width in px (default: board width + 80)."},
                "height": {"type": "integer", "description": "Viewport height in px (default: board height + 120)."},
            },
            "required": ["file_id", "page_id"],
        },
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


def _text(value) -> list[TextContent]:
    return [TextContent(type="text", text=value if isinstance(value, str) else json.dumps(value, indent=1))]


async def _with_client(fn):
    client = ps.PenpotClient(ps.load_config())
    try:
        return await fn(client)
    finally:
        await client.aclose()


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    arguments = arguments or {}
    tool = next((t for t in TOOLS if t.name == name), None)
    if tool is not None:
        missing = [k for k in tool.inputSchema.get("required", []) if arguments.get(k) in (None, "", [])]
        if missing:
            return _text(f"Error: missing required argument(s): {', '.join(missing)}")
    try:
        if name == "search_icons":
            if arguments.get("ids"):
                return _text(await ps.icon_artwork(arguments["ids"]))
            return _text(await ps.search_icons(arguments["query"], arguments.get("set"), arguments.get("limit", 24)))

        if name == "build_design":
            result = await _with_client(lambda c: ps.build_tree(
                c, arguments["file_id"], arguments["page_id"], arguments["nodes"],
                arguments.get("parent_id"), bool(arguments.get("dry_run"))))
            if result["attributions"]:
                result["add_attribution"] = (
                    "These artworks need a credit: add a small text line such as '"
                    + "; ".join(result["attributions"]) + "' on the board or in the handoff notes.")
            return _text(result)

        if name == "move_shapes":
            return _text(await _with_client(lambda c: ps.move_shapes(
                c, arguments["file_id"], arguments["page_id"], arguments["shape_ids"], arguments["parent_id"])))

        if name == "inspect_design":
            async def inspect(c):
                file = await c.get_file(arguments["file_id"])
                if not arguments.get("page_id"):
                    pages = (file.get("data") or {}).get("pagesIndex") or {}
                    return {"file": file.get("name"), "revn": file.get("revn"), "pages": [
                        {"id": pid, "name": p.get("name"), "boards": ps.find_boards(file, pid)}
                        for pid, p in pages.items()]}
                return ps.describe_page(file, arguments["page_id"], arguments.get("frame_id"))
            return _text(await _with_client(inspect))

        if name == "render_preview":
            with tempfile.TemporaryDirectory(prefix="penpot-render-") as tmp:
                out = os.path.join(tmp, "board.png")
                result = await _with_client(lambda c: ps.render_board(
                    c, arguments["file_id"], arguments["page_id"], arguments.get("frame_id"), out,
                    width=arguments.get("width"), height=arguments.get("height")))
                with open(out, "rb") as fh:
                    data = base64.b64encode(fh.read()).decode("ascii")
            summary = {k: result[k] for k in ("board", "width", "height")}
            return [
                TextContent(type="text", text="Rendered board (Penpot viewer). " + json.dumps(summary)),
                ImageContent(type="image", data=data, mimeType="image/png"),
            ]

        return _text(f"Unknown tool: {name}")
    except ps.PenpotError as exc:
        return _text(f"Error: {exc}")
    except SvgError as exc:
        return _text(f"Error: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
