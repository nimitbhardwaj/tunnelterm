/* tunnelterm front-end entrypoint.
 *
 * As of v0.1.4, the session token is stored exclusively in an HttpOnly
 * cookie set by the server. The client never sees the token: it just
 * POSTs the password to /api/auth and the browser handles transport
 * for /api/verify, /api/logout, and the /ws upgrade.
 *
 * Source layout:
 *   constants.js   - immutable values, PREFS_DEFAULTS
 *   state.js       - mutable runtime state + DOM element cache
 *   api.js         - HTTP client for /api/* endpoints
 *   themes.js      - load themes.json + fonts.json
 *   status.js      - status bar / latency display
 *   terminal.js    - xterm.js construction
 *   ws.js          - WebSocket bridge to /ws
 *   settings.js    - drawer + applier functions
 *   ui/dom.js      - escapeHtml etc.
 *   ui/overlay.js  - in-terminal modal
 *   ui/search.js   - find bar
 *   ui/mobile.js   - mobile virtual key-bar
 *   ui/login.js    - login form + enterTerminal()
 */
"use strict";

import { verifySession } from "./api.js";
import { $, els } from "./state.js";
import { startStatusClock } from "./status.js";
import { refit } from "./terminal.js";
import { loadThemesAndFonts } from "./themes.js";
import { setupMobileBar } from "./ui/mobile.js";
import { enterTerminal, wireLogin } from "./ui/login.js";
import { openSearch, wireSearch } from "./ui/search.js";

function cacheDom() {
  Object.assign(els, {
    login:              $("login"),
    terminal:           $("terminal"),
    loginForm:          $("login-form"),
    passwordInput:      $("password"),
    showPwBtn:          $("show-pw"),
    loginError:         $("login-error"),
    statusDot:          $("status-dot"),
    statusText:         $("status-text"),
    latencyEl:          $("status-latency"),
    sessionTimer:       $("status-timer"),
    settingsBtn:        $("settings-btn"),
    shortcutsBtn:       $("shortcuts-btn"),
    searchOpenBtn:      $("search-open-btn"),
    drawer:             $("settings-drawer"),
    drawerScrim:        $("drawer-scrim"),
    drawerClose:        $("drawer-close"),
    themeGrid:          $("theme-grid"),
    fontSelect:         $("font-select"),
    fontSizeSlider:     $("font-size-slider"),
    fontSizeValue:      $("font-size-value"),
    letterSpacing:      $("letter-spacing"),
    letterSpacingValue: $("letter-spacing-value"),
    lineHeight:         $("line-height"),
    lineHeightValue:    $("line-height-value"),
    cursorBlink:        $("cursor-blink"),
    scrollback:         $("scrollback"),
    copyOnSelect:       $("copy-on-select"),
    macOptionAsMeta:    $("mac-option-as-meta"),
    screenReaderMode:   $("screen-reader-mode"),
    clearBtn:           $("clear-btn"),
    downloadBtn:        $("download-btn"),
    logoutBtn:          $("logout-btn"),
    resetBtn:           $("reset-btn"),
    term:               $("term"),
    searchBar:          $("search-bar"),
    searchInput:        $("search-input"),
    searchPrev:         $("search-prev"),
    searchNext:         $("search-next"),
    searchClose:        $("search-close"),
    shortcutsModal:     $("shortcuts-modal"),
    shortcutsClose:     $("shortcuts-close"),
    mobileBar:          $("mobile-bar"),
  });
}

async function boot() {
  cacheDom();
  await loadThemesAndFonts();
  startStatusClock();

  wireLogin();
  wireSearch();
  setupMobileBar();
  els.searchOpenBtn.onclick = openSearch;

  // Re-fit xterm on viewport changes.
  window.addEventListener("resize", () => refit());

  // Right-click paste, gated by preference.
  const { state } = await import("./state.js");
  els.term.addEventListener("contextmenu", (e) => {
    if (!state.prefs.rightClickPaste) return;
    e.preventDefault();
    navigator.clipboard.readText().then((t) => {
      if (t && state.ws && state.ws.readyState === 1) state.ws.send(t);
    }).catch(() => { /* swallow */ });
  });

  // Auto-login if the cookie is still valid.
  if (await verifySession()) {
    await enterTerminal();
    return;
  }
  els.passwordInput.focus();
}

// Module scripts always defer, so DOM parsing is finished by the time we run.
// If the event has already fired we'd never see it via addEventListener.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
