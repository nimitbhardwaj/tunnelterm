/* Constants shared across the frontend.
 *
 * The token is no longer stored client-side: as of v0.1.4 the server sets an
 * HttpOnly cookie, and the browser handles transport automatically. The
 * "Remember me" checkbox is now translated into a cookie Max-Age on the
 * server side, not a localStorage flag here.
 */
"use strict";

export const CTRL = "__tt";
export const PREFS_KEY = "tunnelterm:prefs";

export const PREFS_DEFAULTS = {
  theme: null,                  // resolved from themes.json default
  fontFamily: null,             // resolved from fonts.json default
  fontSize: 14,
  fontWeight: "normal",         // normal | bold
  letterSpacing: 0,
  lineHeight: 1.2,
  cursorStyle: "block",         // block | underline | bar
  cursorBlink: true,
  scrollback: 5000,
  bell: "none",                 // none | sound | visual
  copyOnSelect: false,
  rightClickPaste: false,
  macOptionAsMeta: false,
  screenReaderMode: false,
};

// Cap scrollback locally too -- malicious extensions could otherwise OOM
// the renderer with state.term.options.scrollback = 1e9.
export const SCROLLBACK_MAX = 100000;
