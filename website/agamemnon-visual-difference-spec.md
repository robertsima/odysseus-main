# Agamemnon visual-difference specification

Reference: Penpot file `77fbae64-173c-81f5-8008-b823d468d03d`, page `b44baf9e-64a9-4d69-958a-d07e52b9d89b`; boards Chat, Agents, and Workbench.

## Differences observed before implementation

The merged theme work supplied the charcoal/gold palette, a crest, theme switching, and compact role seals, but the application still retained the Odysseus composition: the brand was a small centered sidebar banner, navigation remained an undifferentiated product list, chat had no strategy-room heading or context rail, Agents remained a floating telemetry window, and Workbench remained a tabbed utility modal. Typography remained Fira Code rather than the mockup's Space Grotesk. The existing role seals were generic shield/helmet constructions rather than artwork that visually belongs to the mockup's restrained gold command system.

## Acceptance criteria (visual, at 1440 × 920)

1. **Theme isolation and switching.** With Agamemnon selected, the root has a 240 px command rail and the three principal surfaces use the mockup composition. With Odysseus selected, none of those layout overrides apply and its existing geometry, palette, logo treatment, and interactions remain unchanged. Repeated switching does not leave Agamemnon-only headings or layout classes behind.
2. **Identity and typography.** Agamemnon uses the exact Penpot crest path, `#d7b35a` gold, and self-hosted Space Grotesk. Its wordmark is uppercase, 20 px/700, alongside a 39.375 × 29.53125 crest. The page background is `#111417`; rail is `#171c21`; primary panels are `#1b2127`; raised panels are `#20262c`; structural borders are `#343d45`/`#59636d`.
3. **Chat / Strategy Room.** The screen reads as three columns: 240 px command rail, transcript/composer workspace, and 306 px context rail. The heading reads `CHAT / STRATEGY ROOM`; transcript and composer use 2 px gold keylines; the answer is a distinct raised card; the context rail exposes session state, controls, evidence, and customization.
4. **Agents / Phalanx.** The heading reads `AGENT SPACE / PHALANX`. Four agent cards form a two-column grid, with Commander highlighted gold, ordinary cards slate, and an attention card coral. Each card has a real vector role mark (command, implement, scout, review), uppercase state, role/activity text, and compact actions.
5. **Workbench / Run Control.** The heading reads `WORKBENCH / RUN CONTROL`. The main column contains a gold-keylined run summary, timeline panel, and dark output console; a 306 px detail rail shows agent, start time, status, activity, and customization.
6. **Artwork integrity.** Crest and role marks are SVG paths/assets, not CSS-drawn substitutes. Local asset previews render without clipping, overlap, or missing glyphs.
7. **Responsive behavior.** Below desktop width, the auxiliary context/detail rail collapses below the main content and card grids become one column; controls remain legible. Existing narrow-screen Odysseus behavior is unaffected.
8. **No regressions.** Existing theme persistence/customization paths still work, Agamemnon is still the default `dark` identity, and focused theme/layout tests pass.
