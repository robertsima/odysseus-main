/* Shared by the parser-blocking first-paint script and theme.js. Keep this
 * dependency-free and classic-script compatible: preferences must paint
 * before the module graph or an account request finishes. */
globalThis.AgamemnonAppearance = Object.freeze({
  fontFamily(font = 'auto') {
    const fonts = {
      auto: null,
      mono: "'Fira Code', monospace",
      grotesk: "'Space Grotesk', system-ui, sans-serif",
      sans: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      serif: "Georgia, 'Times New Roman', serif",
      humanist: "'Trebuchet MS', 'Segoe UI', sans-serif",
      editorial: "Palatino, 'Palatino Linotype', 'Book Antiqua', Georgia, serif",
      classic: "'Times New Roman', Times, serif",
      code: "'Cascadia Code', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace",
      rounded: "ui-rounded, 'Arial Rounded MT Bold', system-ui, sans-serif",
      opendyslexic: "'OpenDyslexic', sans-serif",
    };
    if (Object.hasOwn(fonts, font)) return fonts[font];
    return `'${String(font).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/[\r\n\f]/g, ' ')}', sans-serif`;
  },
  pageStyle(entry, theme) {
    if (entry?.value === 'classic' || entry?.value === 'agamemnon') return entry.value;
    return !theme || theme.name === 'dark' ? 'agamemnon' : 'classic';
  },
});
