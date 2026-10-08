# Appearance boundary

`data-style` selects **composition**, not feature availability. Classic and
Agamemnon share the DOM, state and handlers. `data-theme` and the five base
color variables select the **palette** independently of composition.

## Ownership

- `appearancePreferences.js`: dependency-free font and legacy style resolution.
  The parser-blocking first-paint script and `theme.js` use the same resolver.
  Add a font here once, not in the inline bootstrap too.
- `theme.js`: apply/save palette, font, scale, density and effects. Stock palette
  swatches keep the chosen font; saved custom themes may bundle a font. Choosing
  "Theme default" restores Space Grotesk for Agamemnon or Fira Code for Classic.
- `serverPrefs.js`: account reconciliation and timestamps. Newer preferences
  win, with user ownership checked before uploading browser data. Composition
  has its own preference key; changing a palette must not switch composition.
- `style.css`: shared feature styles and `--ui-font`. `--ui-chrome-font` aliases
  that same font for panels, modals and controls at every breakpoint.
- `agamemnon-mockup.css`: composition and skin. Its `--ag-*` tokens derive from
  shared palette variables. Do not hard-code feature fonts or hide shared
  interactive nodes and replace them with decorative copies.
- `chatHeader.js`: moves the single interactive `#current-meta` between the
  header compositions. `app.js` owns session actions/rename; `sessions.js` owns
  title updates. `agamemnonChatIdentity.js` only decorates busy status.
- `chat.js`, `chatRenderer.js`, `roundTiming.js`: stream/history, round durations
  and total completed-turn wall time. Neither renderer branches on appearance.
  The same `.agent-turn-duration` remains visible after a style switch.

## Font exceptions

Code, source editors, terminal/log/diff output, identifiers and numeric agent
telemetry keep monospace (`--mono-font`, tabular numbers). Rich email/document
content can use its own content typography and emoji fallbacks. The branding
wordmark uses Space Grotesk; it is artwork, not an interactive UI control.
New controls inherit `--ui-font`; do not add a literal font stack to a skin.

## Extending a feature

Implement the behavior in its existing module and shared node once. Supply a
skin token or move that node only when composition requires it. Check both
styles at desktop/half/mobile; font selection, stock palette switch, reload,
account reconciliation and session actions are covered in
`tests/static/js/theme/test_shared_appearance.py` and the theme account tests.
Existing browser tests also exercise shared Workbench docks/tabs, Phalanx run
controls, settings, composer and streamed history. Add a behavioral regression
to the mirrored module test folder for a new capability.
