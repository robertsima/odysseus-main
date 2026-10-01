---
name: visual-asset-sourcing
description: Logos, icons, sprites, mascots, avatars, emblems and illustrations. Source real licensed artwork instead of hand-drawn SVG, and let the user pick between rendered options.
metadata:
  version: 1.0.0
  category: design
  status: published
  source: bundled
---

# Visual asset sourcing

Language models draw crude shapes: a logo or figure built from hand-written path coordinates comes out basic and hard to read, and review rounds do not fix that. Start from real artwork. Hand-authored SVG is for simple geometric UI glyphs only (chevron, plus, dot, divider).

## Source the artwork

1. **Project and user art first.** If the repository or the user already has the artwork, use it.
2. **Search.** `mcp__penpot_studio__search_icons` with a `set` filter, such as `game-icons` for helmets, weapons, creatures and figures. Try synonyms (helmet, crested-helmet, spartan, centurion). Without that tool, query `https://api.iconify.design/search?query=helmet&prefix=game-icons`.
3. **Get the SVG.** Call `search_icons` again with `ids=[...]` (max 6) for the markup, licence, author and attribution line. Fallback: `web_fetch` or `bash curl` on `https://api.iconify.design/<prefix>/<name>.svg`.
4. **Check the licence.** CC0, MIT, Apache and ISC need no credit. CC BY needs attribution (game-icons.net is CC BY 3.0). Non-commercial and share-alike sets need the project's existing approval.
5. **Record attribution** where the project keeps credits (for example `ACKNOWLEDGMENTS.md`) and beside the asset: icon id, author, licence, source URL.
6. **Recolor from CSS.** Set `fill="currentColor"` (or a CSS variable) so themes and dark mode work, and leave the paths as sourced.

## Distinct identities need distinct artwork

Characters, factions and sprites differ in silhouette: use different figures (`game-icons:spartan`, `swordman`, `bowman`, `archer`) or poses. Colour is an accent on a different shape, never the only difference.

## Taste goes to the user

A logo or mascot's look is the user's call. Reviewers (agents) check defects such as clipping, contrast and wrong size.

1. Build a candidate sheet of 4 to 6 options side by side, at the sizes they will really appear, on the real background. Render it with `preview_file`.
2. End your message with a `Needs user:` line asking which to use.
3. Proceed with the best option only if the user said not to wait. Then say which you chose, why, and that it is easy to swap.

## Verify at real sizes

Render with `preview_file` at the smallest and largest real size (an icon at 16, 24 and 32 px; a brand mark at its large header size), on light and dark backgrounds. The shape must still read at the small size and the recolouring must work in both themes.

## Mockups

An agent-drawn mockup is a layout reference, and its artwork is a placeholder unless the user says it is final. "Follow the mockup exactly" covers layout, spacing and colour. Replace crude placeholder drawings with sourced art and say so.

## Handoff

List the icon ids used, their licence and where the attribution lives, the candidate sheet you showed, the sizes and backgrounds you checked, and any `Needs user:` decision still open.
