/* xterm.js construction + glyph-cache management.
 *
 * All globals (Terminal, FitAddon, ...) come from the script tags in
 * index.html; we don't bundle xterm. */
"use strict";

import { CTRL } from "./constants.js";
import { els, state } from "./state.js";
import { findTheme } from "./themes.js";
import { clearStickyMods } from "./ui/mobile.js";
import { closeSearch, openSearch } from "./ui/search.js";

/** Build the comma-separated CSS font-family stack with PUA icon fallback. */
export function buildFontStack(family) {
  return `"${family}", "Symbols Nerd Font", ui-monospace, "SF Mono", Menlo, monospace`;
}

/** Block until the requested font is loaded so xterm measures the right glyph width. */
export async function ensureFontLoaded(family, size) {
  if (!document.fonts || !document.fonts.load) return;
  try {
    await document.fonts.load(`${size}px "${family}"`);
    await document.fonts.load(`bold ${size}px "${family}"`);
    await document.fonts.load(`16px "Symbols Nerd Font"`);
  } catch (e) {
    console.warn("font load failed:", e);
  }
}

/**
 * Force xterm to re-rasterize all glyphs after a font/size/weight change.
 * Uses the public clearTextureAtlas() API (xterm 5.3+).
 */
export async function rebuildGlyphCache() {
  if (!state.term) return;
  await ensureFontLoaded(state.prefs.fontFamily, state.prefs.fontSize);
  await new Promise((r) => requestAnimationFrame(() => r()));
  try { state.term.clearTextureAtlas(); } catch { /* older xterm */ }
  if (state.fitAddon) {
    try { state.fitAddon.fit(); } catch { /* not yet open */ }
  }
  try { state.term.refresh(0, state.term.rows - 1); } catch { /* not yet open */ }
}

/** Refit, deferred until any newly-requested fonts are ready. */
export function refit() {
  const family = state.prefs.fontFamily;
  if (document.fonts && family) {
    document.fonts.ready
      .then(() => { state.fitAddon && state.fitAddon.fit(); })
      .catch(() => { /* swallow */ });
  } else {
    state.fitAddon && state.fitAddon.fit();
  }
}

/**
 * Build the xterm instance + load addons. The caller is expected to also
 * call connect() afterwards.
 */
export async function buildTerminal({ onFontSizeNudge }) {
  await ensureFontLoaded(state.prefs.fontFamily, state.prefs.fontSize);

  const theme = findTheme(state.prefs.theme);
  state.term = new Terminal({
    cursorBlink: state.prefs.cursorBlink,
    cursorStyle: state.prefs.cursorStyle,
    fontSize: state.prefs.fontSize,
    fontFamily: buildFontStack(state.prefs.fontFamily),
    fontWeight: state.prefs.fontWeight,
    letterSpacing: state.prefs.letterSpacing,
    lineHeight: state.prefs.lineHeight,
    scrollback: state.prefs.scrollback,
    macOptionIsMeta: state.prefs.macOptionAsMeta,
    screenReaderMode: state.prefs.screenReaderMode,
    bellStyle: state.prefs.bell === "sound" ? "sound" : "none",
    theme: theme ? theme.colors : undefined,
    allowProposedApi: true,
  });

  state.fitAddon = new FitAddon.FitAddon();
  state.term.loadAddon(state.fitAddon);

  if (window.WebLinksAddon) {
    state.term.loadAddon(new WebLinksAddon.WebLinksAddon());
  }
  if (window.SearchAddon) {
    state.searchAddon = new SearchAddon.SearchAddon();
    state.term.loadAddon(state.searchAddon);
  }
  if (window.Unicode11Addon) {
    state.term.loadAddon(new Unicode11Addon.Unicode11Addon());
    state.term.unicode.activeVersion = "11";
  }
  if (window.ClipboardAddon) {
    state.term.loadAddon(new ClipboardAddon.ClipboardAddon());
  }
  if (window.SerializeAddon) {
    state.serializeAddon = new SerializeAddon.SerializeAddon();
    state.term.loadAddon(state.serializeAddon);
  }

  state.term.open(els.term);

  try {
    if (window.WebglAddon) {
      const addon = new WebglAddon.WebglAddon();
      addon.onContextLoss(() => {
        try { addon.dispose(); } catch { /* swallow */ }
        if (state.webglAddon === addon) state.webglAddon = null;
      });
      state.term.loadAddon(addon);
      state.webglAddon = addon;
    }
  } catch (e) {
    console.warn("WebGL renderer unavailable:", e);
  }

  state.fitAddon.fit();

  state.term.onTitleChange((title) => {
    if (title) document.title = title + " · tunnelterm";
  });

  state.term.onBell(() => {
    if (state.prefs.bell === "visual") {
      els.term.animate(
        [{ filter: "brightness(1)" }, { filter: "brightness(1.6)" }, { filter: "brightness(1)" }],
        { duration: 200 },
      );
    }
  });

  state.term.onSelectionChange(async () => {
    if (state.prefs.copyOnSelect) {
      const sel = state.term.getSelection();
      if (sel) try { await navigator.clipboard.writeText(sel); } catch { /* swallow */ }
    }
  });

  state.term.onData((data) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(data);
      clearStickyMods();
    }
  });

  state.term.onResize(({ cols, rows }) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ [CTRL]: "resize", cols, rows }));
    }
  });

  state.term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "f") {
      openSearch();
      return false;
    }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "c") {
      const sel = state.term.getSelection();
      if (sel) {
        navigator.clipboard.writeText(sel).catch(() => { /* swallow */ });
        return false;
      }
    }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "v") {
      navigator.clipboard
        .readText()
        .then((t) => { if (state.ws?.readyState === 1) state.ws.send(t); })
        .catch(() => { /* swallow */ });
      return false;
    }
    if ((e.ctrlKey || e.metaKey) && (e.key === "+" || e.key === "=")) {
      onFontSizeNudge(state.prefs.fontSize + 1);
      return false;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "-") {
      onFontSizeNudge(state.prefs.fontSize - 1);
      return false;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "0") {
      onFontSizeNudge(14);
      return false;
    }
    if (e.key === "Enter" && e.shiftKey) {
      state.ws?.readyState === 1 && state.ws.send("\n");
      return false;
    }
    if (e.key === "Escape" && state.searchOpen) {
      closeSearch();
      return false;
    }
    return true;
  });
}
