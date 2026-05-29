/* Shared mutable state for the tunnelterm frontend.
 *
 * One object exported as a named binding so every module mutates the same
 * instance (ES modules are evaluated once and cached).
 */
"use strict";

import { PREFS_DEFAULTS, PREFS_KEY } from "./constants.js";

export function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    return raw ? { ...PREFS_DEFAULTS, ...JSON.parse(raw) } : { ...PREFS_DEFAULTS };
  } catch {
    return { ...PREFS_DEFAULTS };
  }
}

export function savePrefs(p) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* private mode */ }
}

/** The single source of truth for runtime state. */
export const state = {
  prefs: loadPrefs(),
  // xterm + addons
  term: null,
  fitAddon: null,
  searchAddon: null,
  serializeAddon: null,
  webglAddon: null,
  // WebSocket
  ws: null,
  // theme/font catalogs
  themes: [],
  fonts: [],
  fontsDefault: "ui-monospace, monospace",
  themesDefault: "tokyo-night",
  // connection mgmt
  reconnectAttempt: 0,
  reconnectTimer: null,
  wantConnected: false,
  sessionStartTs: 0,
  latencyMs: null,
  lastPingTs: 0,
  pingTimer: null,
  // ui
  drawerOpen: false,
  searchOpen: false,
  // sticky modifiers for mobile keybar
  stickyCtrl: false,
  stickyAlt: false,
  stickyShift: false,
  // UI-build guard so we don't re-init xterm on auto-login.
  terminalUiBuilt: false,
};

/** DOM element cache, populated by app.js at boot. */
export const els = {};

/** Convenience: shortcut for document.getElementById. */
export const $ = (id) => document.getElementById(id);
