# Agamemnon visual verification

Compared at 1440 × 920 against Penpot file `77fbae64-173c-81f5-8008-b823d468d03d`, page `b44baf9e-64a9-4d69-958a-d07e52b9d89b`. Penpot's inspector supplied exact dimensions, colors, type, paths, and a clean layout report for all three boards. Penpot image rendering itself returned its generic error surface on each board, so the implementation was compared visually against the inspected geometry and the local renders below.

| Criterion | Render/evidence | Status |
|---|---|---|
| Theme isolation and switching | Production rules are identity-scoped; Agamemnon and Odysseus brand labels are mutually selected. Existing toggle/layout tests exercise both identities. | Match |
| Identity and typography | `agamemnon-hoplitic-crest.svg`; the exact crest path is embedded in each static preview so it renders without an external-file dependency; self-hosted Space Grotesk 400/700; canonical `#d7b35a`, `#343d45`, `#59636d` tokens. | Match |
| Chat / Strategy Room | `agamemnon-preview-chat.html`, rendered 1440 × 920 after final fixes. 240/790/306 composition, 590 px transcript, 254 px raised answer, 104 px composer. | Match |
| Agents / Phalanx | `agamemnon-preview-agents.html`, rendered 1440 × 920 after the icon revision. Four 510 × 300 cards at Penpot coordinates; gold/slate/coral states and one coherent Trojan-soldier form in four representative model colors. Production card class/geometry uses the same composition. | Match |
| Workbench / Run Control | `agamemnon-preview-workbench.html`, rendered 1440 × 920 after final fixes. Semantic production summary/timeline/console/detail structures at 790 × 110/330/200 and 306 × 699. | Match |
| Artwork integrity | `agamemnon-hoplitic-crest.svg` and `agamemnon-agent-marks.svg` rendered locally. `agamemnon-role-marks-preview.svg` embeds the generic Trojan soldier—built from the exact Penpot crest silhouette—in default, Anthropic, Google, and OpenAI colors without clipping or external-reference failures. Role remains text; model family alone drives icon color. Odysseus still uses its prior role seals. | Match |
| Responsive behavior | Production CSS puts context/run details below the main column at ≤1050 px and cards in one column; ≤700 px reduces title sizing. | Match by CSS contract |
| No regressions | 59 focused theme/layout/Agents tests pass; JavaScript syntax checks for theme, Agents, and Workbench plus `git diff --check` pass. | Match |

## Independent critic and fixes

The UI Design Critic found that the first pass's preview geometry was offset, production Agents selectors missed generated cards, Workbench detail was CSS-generated, Chat lacked assistant-card styling, palette tokens drifted, Scout artwork was missing, narrow rails disappeared, and Agamemnon identity could leak into Odysseus. Every actionable finding was addressed: exact preview coordinates; a stable production `ag-card` class and full-width Phalanx grid; semantic Run Control DOM; real `.msg-ai` treatment; one canonical palette; a shared Scout vector; below-content responsive rails; accessible visible Chat H1; and mutually exclusive theme brand labels.

After user review, the role-pictogram shields were replaced in Agamemnon with a single polished Trojan-soldier family. Its helmet reuses the supplied Penpot crest path exactly and adds a restrained nose/cheek guard for readability at 28–58 px. A canonical resolver makes explicit model identity authoritative and uses execution source only as fallback; model families drive a restrained color system while status and role remain explicit text. Production Chat Session Context, Agent cards, and Workbench Run Details all use the same sprite. The prior Odysseus seals remain available and unchanged.

The independent UI Design Critic initially returned **PASS WITH CHANGES**. Its actionable findings were fixed: mixed model/source inputs no longer miscolor icons; Chat and Workbench production DOM now match their fixtures; the exact full-precision Penpot path replaces the rounded approximation; a historical face-guard cue improves Trojan readability; and the sprite now has a root-level use so it renders directly. A final critic pass identified Chat synchronization and fixture drift; Chat now listens for session/model changes through the shared resolver, and Chat/Workbench fixtures reference the canonical sprite instead of duplicate paths. Local `preview_file` captures confirmed the sprite and all three 1440 × 920 fixtures render without clipping. Penpot image rendering still returns its generic error surface; Penpot structural inspection remains the reference fallback.

## Residual differences

- Penpot's preview renderer returned an error surface, so direct image-overlay/pixel-diff evidence could not be produced. Penpot `inspect_design` succeeded for all boards and reported no overlap, overflow, or no-render issues; exact inspected values were used instead.
- Static previews use deterministic fixture copy so backend state is not required. Production surfaces retain real dynamic chat, agent, and activity data, so copy and card count vary at runtime while geometry and visual hierarchy remain theme-specific.
