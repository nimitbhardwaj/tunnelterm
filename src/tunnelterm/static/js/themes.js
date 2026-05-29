/* Theme + font catalog loading. */
"use strict";

import { state } from "./state.js";

export async function loadThemesAndFonts() {
  try {
    const [tRes, fRes] = await Promise.all([
      fetch("/static/themes.json", { credentials: "same-origin" }),
      fetch("/static/fonts.json", { credentials: "same-origin" }),
    ]);
    const t = await tRes.json();
    const f = await fRes.json();
    state.themes = Array.isArray(t.themes) ? t.themes : [];
    state.themesDefault =
      t.default || (state.themes[0] && state.themes[0].name) || "tokyo-night";
    state.fonts = Array.isArray(f.fonts) ? f.fonts : [];
    state.fontsDefault =
      f.default || (state.fonts[0] && state.fonts[0].family) || "ui-monospace, monospace";
    if (!state.prefs.theme) state.prefs.theme = state.themesDefault;
    if (!state.prefs.fontFamily) state.prefs.fontFamily = state.fontsDefault;
  } catch (err) {
    console.error("Failed to load themes/fonts:", err);
  }
}

export function findTheme(name) {
  return state.themes.find((t) => t.name === name) || state.themes[0];
}
