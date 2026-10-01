---
name: penpot-design-workflow
description: Penpot designs, created, edited or inspected. Build nested layouts with real vector icons, then render and check them with the Penpot Studio tools.
metadata:
  version: 1.0.0
  category: design
  status: published
  source: bundled
---

# Penpot design workflow

The stock Penpot tools cannot nest shapes, import artwork or show you what you made. The Penpot Studio tools (`mcp__penpot_studio__*`) can, so every Penpot task goes through the loop below.

## The loop: read, build, look, fix

1. **Read what exists.** `inspect_design(file_id)` lists pages and boards. With a `page_id` it returns every shape (position, size, fill, stroke, text, fonts), the colour palette and a layout check. `render_preview` a board to see it. Do this before editing an existing board, so new work matches the real product.
2. **Protect the original.** Take a file snapshot (`create_file_snapshot`) and build on a new, clearly named page (`add_page`). Existing pages stay as the user left them.
3. **Fix the tokens once, in your plan.** Palette of 3-5 colours with one accent, at most two fonts, one border width, one shadow offset. Reuse them in every node.
4. **Source artwork.** For any logo, mascot, helmet, sword, avatar or icon, `search_icons` and place the result as an `icon` node. Use one icon family for UI icons and a richer set (`game-icons`) for emblems and sprites. Icons are monotone vectors: set `color`, and layer two icons or shapes for a two-tone sprite. For logos, mascots and figures follow `visual-asset-sourcing`: show the user 4-6 rendered options and let them pick. `build_design` returns the credit line for sets that need attribution; put it in a small text line on the board or in the handoff.
5. **Build one board per `build_design` call.** Coordinates are relative to the parent. Size every container first, then place children with margins you can add up: 16-24px padding, one grid, and a `w` on any text that wraps.
6. **Look.** `render_preview` the board and read the picture for clipped or overlapping text, icons that do not read, low contrast and empty areas. `inspect_design` with the `page_id` must report no OVERFLOW, OVERLAP or NO-RENDER problems; fix each one it lists. A render showing Penpot's error screen is a failed render: report it and count the design as unseen.
7. **Fix in one focused pass.** `delete_shape` the faulty shapes and rebuild just that part with `build_design` (`parent_id` = the board), or rebuild the board on a fresh page. Render again. If the third render still shows the same defect, stop and report it.

## Rules that save rounds

- **Text comes from `build_design`.** Stock `create_text` stores no layout data, so previews and exports draw nothing for it and its size is a guess. To change a built text, rebuild the node; `update_shape` on its content leaves the layout data stale.
- **Nest with `build_design`.** Stock `create_rectangle`, `create_circle` and `create_frame` always write to the page root. Use `move_shapes` to rescue shapes that already exist.
- **Fonts** are Google fonts by family name (`family:"Space Grotesk"`). A misspelt family falls back to a default, so check the render.
- **Contrast**: body text 4.5:1 against its fill, large text 3:1. Pair status colour with a label or an icon.
- **Neo-brutalism** means thick solid borders (3-4px, `stroke` `align:"inner"`), hard offset shadows with `blur:0` (`{x:6,y:6,color:"#000"}`), flat saturated fills, square or tiny radii, heavy grotesque headings (Space Grotesk 700-800 or Archivo Black), uppercase letter-spaced labels and high contrast. Name layers and keep fills to few distinct values so the palette can be swapped.

## Handoff

Report:
- file, page and board IDs, and what you built;
- the render you viewed and what you fixed after viewing it;
- remaining layout problems (ideally none);
- fonts, palette, icon sources, licences and the attribution line;
- what is unverified. A render shows what Penpot draws, so responsive behaviour, keyboard order and screen-reader output are always unverified.

Report READY only when you viewed a render and `inspect_design` shows no OVERFLOW, OVERLAP or NO-RENDER problems.
