---
name: visual-asset-sourcing
description: Use for any logo, icon, sprite, mascot, avatar, emblem, figure or illustration work. Source real licensed artwork (Iconify sets such as game-icons, the project's own assets, or art the user supplies) instead of hand-drawing SVG paths, and let the user pick between rendered options.
metadata:
  version: 1.0.0
  category: design
  status: published
  source: bundled
---

# Visual asset sourcing

Language models draw crude shapes. A logo or figure built from hand-written
path coordinates comes out basic and hard to read, and no number of review
rounds fixes that. Start from real artwork.

## When to use this

Any task that needs a logo, brand mark, mascot, character, sprite set, avatar,
emblem, icon set or illustration. Hand-authored SVG is fine only for simple
geometric UI glyphs (a chevron, a plus, a dot, a divider).

## Source the artwork

1. **Search.** `mcp__penpot_studio__search_icons` with a `set` filter, e.g.
   `game-icons` for helmets, weapons, creatures and figures. Try several
   queries and synonyms (helmet, crested-helmet, spartan, centurion). Without
   that tool, use the Iconify search API (`https://api.iconify.design/search?query=helmet&prefix=game-icons`).
2. **Get the SVG** of the chosen ids: `search_icons` with its SVG option,
   which returns the markup plus the licence and attribution. Fallback:
   `web_fetch` or `bash curl` on `https://api.iconify.design/<prefix>/<name>.svg`.
3. **Check the licence** before using it. CC0, MIT, Apache and ISC need no
   credit; CC BY (game-icons.net is CC BY 3.0) requires attribution. Skip
   non-commercial or share-alike sets unless the project already allows them.
4. **Record attribution** where the project keeps credits (for example
   `ACKNOWLEDGMENTS.md`) and in a comment or README next to the asset: icon
   id, author, licence, source URL.
5. **Recolor without editing paths.** Set `fill="currentColor"` (or a CSS
   variable) and colour from CSS, so themes and dark mode work. Do not
   redraw or "improve" the paths by hand.
6. **Keep the project's own assets and user-supplied art first.** If the
   repository or the user already has the artwork, use it.

## Distinct identities need distinct artwork

Several characters, factions or sprites must differ in silhouette: use
different figures (for example `game-icons:spartan`, `swordman`, `bowman`,
`archer`) or different poses, not one image recoloured. Colour is an accent on
top of a different shape, never the only difference.

## Taste goes to the user

Whether a logo or mascot looks good is the user's call, not a critic agent's.

1. Build a candidate sheet: 4 to 6 options side by side, at the sizes they
   will really appear, on the real background. Render it with `preview_file`.
2. End your message with a `Needs user:` line asking which to use.
3. Proceed with the best one only if the user said not to wait; then say which
   you chose and why, and that it is easy to swap.

Do not loop review agents on taste; each round costs minutes and moves
nothing. Use reviewers for defects (clipping, contrast, wrong size), not taste.

## Verify at real sizes

Render with `preview_file` at the smallest and largest real size (for an icon
16, 24 and 32 px; for a brand mark the large header size), on light and dark
backgrounds. Check that the shape still reads at the small size and that the
recolouring works in both themes.

## Mockups

A mockup an agent drew is a layout reference. Its artwork is a placeholder
unless the user says it is final; "follow the mockup exactly" applies to
layout, spacing and colour, not to crude placeholder drawings. Replace them
with sourced art and say so.

## Handoff

List the icon ids used, their licence and where the attribution lives, the
candidate sheet you showed, the sizes and backgrounds you checked, and any
`Needs user:` decision still open.
