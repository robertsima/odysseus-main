---
name: penpot-design-workflow
description: Build, read back and visually verify Penpot designs with the Penpot Studio tools (nested layouts, real vector icons and logos, layout check, rendered screenshot), instead of stacking primitive shapes.
metadata:
  version: 1.0.0
  category: design
  status: published
  source: bundled
---

# Penpot design workflow

Use this whenever a task creates or changes a Penpot design, or asks you to
look at one. The Penpot Studio tools (`mcp__penpot_studio__*`) exist because
the stock Penpot tools cannot nest shapes, cannot import artwork and cannot
show you what you made.

## The loop: read, build, look, fix

1. **Read what exists.** `inspect_design(file_id)` lists pages and boards;
   with a `page_id` it returns every shape with position, size, fill, stroke,
   text, fonts, the colour palette and a layout check. Then
   `render_preview` a board to actually SEE it. Do this before designing so
   the new work matches the real product, and before editing an existing board.
2. **Protect the original.** Take a file snapshot (`create_file_snapshot`)
   and build in a new, clearly named page (`add_page`); never overwrite the
   user's existing pages.
3. **Decide the tokens once**: palette (3-5 colours, one accent), two fonts at
   most, one border width, one shadow offset. Write them down in your plan and
   reuse them in every node.
4. **Find real artwork.** For any logo, mascot, helmet, sword, avatar or
   icon, call `search_icons` and use the result as an `icon` node. Never
   approximate a figure with circles and rectangles. Prefer one icon family
   for UI icons, and a richer set (e.g. `game-icons`) for emblems and sprites.
   Icons are monotone vectors: set `color`; layer two icons or shapes for a
   two-tone sprite. If a set needs attribution, `build_design` returns the
   credit line: put it in a small text line on the board or in the handoff.
5. **Build one board per call** with `build_design`. Coordinates are relative
   to the parent, so a card at `x:24,y:24` inside a board is always 24px in.
   Size every container first, then place children inside it with margins you
   can add up. Keep 16-24px padding, align to one grid, and leave room for
   text that wraps (give text a `w`).
6. **Look.** `render_preview` the board, then read the picture: clipped or
   overlapping text, icons that do not read, low contrast, empty areas.
   `inspect_design` with the `page_id` reports OVERFLOW, OVERLAP and NO-RENDER
   problems; fix every one.
7. **Fix with one focused pass.** Delete the faulty shapes (`delete_shape`)
   and rebuild just that part with `build_design` (`parent_id` = the board), or
   rebuild the board on a fresh page. Render again. Two render-fix rounds are
   normally enough; stop and report if the third still shows the same defect.

## Rules that save rounds

- **Text must come from `build_design`.** Text made with the stock
  `create_text` has no layout data: previews and exports draw nothing for it,
  and its stored size is a guess. Do not edit a built text's content with
  `update_shape`; rebuild that node.
- **Do not use the stock `create_rectangle`/`create_circle`/`create_frame`** for
  layout: they always write to the page root, so nothing nests. Use
  `build_design` (or `move_shapes` to rescue shapes that already exist).
- **Neo-brutalism** means: thick solid borders (3-4px, `stroke` `align:"inner"`),
  hard offset shadows with `blur:0` (e.g. `{x:6,y:6,color:"#000"}`), flat saturated
  fills, square or tiny radii, heavy grotesque type (Space Grotesk 700-800 or
  Archivo Black for headings), uppercase labels with letter-spacing, and
  high contrast. Keep it customisable: name layers and keep colours in few
  distinct values so the palette can be swapped.
- **Fonts** are Google fonts by family name (`family:"Space Grotesk"`). A
  misspelt family falls back to a default: check the preview.
- **Contrast**: body text 4.5:1 against its fill, large text 3:1. Status must
  not rely on colour alone (add a label or an icon).
- A render shows what Penpot draws; it does not test responsive behaviour,
  keyboard order or screen-reader output. Say so in the handoff.

## Handoff

Report the file, page and board IDs; what you built; the rendered evidence
(say you viewed it, and what you fixed after viewing); the remaining layout
problems (ideally none); fonts, palette and icon sources with licences and the
attribution line; and what is unverified. Report READY only when you viewed a
render and `inspect_design` shows no OVERFLOW/OVERLAP/NO-RENDER problems.
