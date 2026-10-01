# Agamemnon visual verification

Compared at 1440 × 920 against Penpot file `77fbae64-173c-81f5-8008-b823d468d03d`, page `b44baf9e-64a9-4d69-958a-d07e52b9d89b`, boards Chat `51e8f401-6310-4ca6-a1e1-a25037d25953`, Agents `21e06404-5496-43be-8a32-d1e65883ce31`, and Workbench `e8977336-40b5-4f21-b5e8-65a4b27c6d6e`. The Penpot viewer directly rendered all three boards (Chat, Agents, Workbench) at 1440 × 920 on October 1, 2026. I compared each board side-by-side with the corresponding local `preview_file` fixture at the same size. The board viewer rendered dark text against dark card fills, limiting a typography/contrast or pixel-by-pixel comparison; its large-scale geometry, sidebar width, rails, panel locations, borders, and heading hierarchy were visible. The local fixtures use readable light-on-dark text instead.

| Criterion | Render/evidence | Status |
|---|---|---|
| Theme isolation | Agamemnon rules remain under `html[data-theme="dark"]`; regular Odysseus labels, raster branding, role seals, and manifest remain unchanged. | Match |
| Product branding | `static/branding/agamemnon-trojan-helmet.svg` is a polished, gold, unmistakable side-profile helmet. It is used by favicon/login/sidebar/welcome and shared navigation. | Match |
| Control-room sprites | `static/branding/agamemnon-agent-marks.svg` contains five separate silhouettes selected by role (`primary`, `worker`, `scout`, `reviewer`, `specialist`). The engineer and scout now use attributable Game Icons `pikeman` (spear-bearing figure) and `bowman` (bow-bearing figure), replacing a faceted bust and a generic sword-stick figure; at 42 and 64 px each reads as an armed standing person. Runtime `currentColor` is model-driven while geometry, role, model, status, and unit number provide non-color distinctions. | Match |
| Chat restraint | `agamemnon-preview-chat.html` and production Chat use the helmet only in shared branding; Session Context is textual and contains no soldier sprite. | Match |
| Agents / Phalanx | `agamemnon-preview-agents.html` renders mounted command cavalry, standing spear-bearing engineer, bow-bearing scout, and shield-bash reviewer silhouettes in gold, Anthropic coral, Google blue, and OpenAI mint. Production chooses the corresponding role symbol and retains an accessible visible detail/configuration surface. | Match |
| Workbench restraint | `agamemnon-preview-workbench.html` and production Workbench use the helmet only in shared branding; Run Details remains textual and contains no soldier sprite. | Match |
| Browser/install identity | Agamemnon favicon, Apple touch mark, page title, and generated PWA manifest use the canonical helmet and Agamemnon name. Saved non-Agamemnon themes retain Odysseus titles, route identity, raster install icons, and manifest name. | Match |
| Actual-size legibility | Production cards render at 64 px. `agamemnon-role-marks-preview.svg` additionally includes 32, 42, and 64 px checks; visible text remains the authoritative identity at every size. | Match |
| Artwork delivery | Five source-derived symbols are embedded once in production HTML and each role fixture; all soldier `<use>` references target same-document IDs. This fixes the prior blank-card and blank-size-sheet renders caused by external SVG references. Portable HTML fixtures embed the exact helmet SVG as a data URI, fixing their broken image while production serves the original same-origin SVG. Odysseus's legacy marks remain unchanged. `python website/sync_agamemnon_artwork.py` regenerates paths and helmet fixture data from the CC BY source. | Match |
| Responsive behavior | Two-column Chat/Agents/Workbench compositions collapse at 1400 px before their fixed Penpot-width columns overflow; compact/mobile refinements remain at 1050/700 px. Dynamic summary/detail surfaces use minimum rather than fixed heights. | Match by CSS and render contract |

## Independent critic and applied fixes

The first clarification review returned **PASS WITH CHANGES** and drove the earlier responsive, visible-detail, dynamic-height, focus, and documentation fixes. A subsequent review returned **FAIL** because all cards still shared identical soldier geometry and differed mainly by color. That actionable finding is addressed here with five licensed vector silhouettes and runtime role selection. Color remains model-driven but is no longer the only visual distinction. Follow-up critiques found and drove two further corrections: Workbench's secondary operational tabs are visible below Run Control, and browser/install branding now uses the canonical helmet only in Agamemnon while preserving regular Odysseus identity. Static review controls use real buttons and links; production controls retain their existing semantics.

## Rendered evidence

- `static/branding/agamemnon-trojan-helmet.svg` — separate 512 × 512 brand render.
- `website/agamemnon-role-marks-preview.svg` — self-contained model colors and 32/42/64 px size render; the artwork must be visible in standalone SVG preview, without external SVG references.
- `website/agamemnon-preview-agents.html` — 1440 × 920 Phalanx render.
- `website/agamemnon-preview-chat.html` — 1440 × 920 Strategy Room render with no contextual soldier.
- `website/agamemnon-preview-workbench.html` — 1440 × 920 Run Control render with no contextual soldier.

## Known difference

Direct side-by-side comparison with the three successfully rendered Penpot boards is now available. The command rail, headings, two-column Agents grid, Chat context rail, and Workbench detail rail align in composition and approximate placement; local fixtures improve legibility over the board viewer's dark-on-dark text. The viewer includes chrome and renders dark text, so exact pixel overlays, color contrast equivalence, and a production-browser behavior check are not established. The implementation uses attributed Game Icons vectors for the clarified helmet and role-specific soldiers rather than the mockup's earlier ambiguous crest; the engineer and scout are now legible standing pikeman and bowman figures at the production 64 px size. Standalone SVG and HTML previews are local review fixtures, not proof of a live app keyboard/accessibility audit.
