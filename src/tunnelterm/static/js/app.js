/* tunnelterm front-end.
 *
 * Talks to:
 *   GET /                         -> this page (served as text/html)
 *   WS  /auth                     -> {password} -> {token} | {error}
 *   WS  /ws  (Sec-WebSocket-Protocol: tunnelterm.v1.token, <token>)
 *                                 -> binary frames from PTY, text control frames
 *   WS  /logout                   -> {token} -> {ok}
 *
 * Server -> client control frames carry the discriminator key "__tt".
 * Client -> server control frames likewise carry "__tt".
 */
"use strict";

// ---------- constants ----------
const SUBPROTOCOL = "tunnelterm.v1.token";
const CTRL = "__tt";
const PREFS_KEY = "tunnelterm:prefs";
const TOKEN_KEY = "tunnelterm:token";
const PREFS_DEFAULTS = {
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
  rememberSession: false,
};

// ---------- DOM helpers ----------
const $ = (id) => document.getElementById(id);
const els = {};

// ---------- prefs ----------
function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    return raw ? { ...PREFS_DEFAULTS, ...JSON.parse(raw) } : { ...PREFS_DEFAULTS };
  } catch { return { ...PREFS_DEFAULTS }; }
}
function savePrefs(p) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch {}
}

// ---------- state ----------
const state = {
  prefs: loadPrefs(),
  term: null,
  fitAddon: null,
  searchAddon: null,
  serializeAddon: null,
  webglAddon: null,
  ws: null,
  wsUrl: null,
  authToken: null,
  themes: [],
  fonts: [],
  fontsDefault: "ui-monospace, monospace",
  themesDefault: "tokyo-night",
  // connection management
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
};

// ============================================================
// Themes & Fonts JSON
// ============================================================
async function loadThemesAndFonts() {
  try {
    const [tRes, fRes] = await Promise.all([
      fetch("/static/themes.json"),
      fetch("/static/fonts.json"),
    ]);
    const t = await tRes.json();
    const f = await fRes.json();
    state.themes = Array.isArray(t.themes) ? t.themes : [];
    state.themesDefault = t.default || (state.themes[0] && state.themes[0].name) || "tokyo-night";
    state.fonts = Array.isArray(f.fonts) ? f.fonts : [];
    state.fontsDefault = f.default || (state.fonts[0] && state.fonts[0].family) || "ui-monospace, monospace";
    if (!state.prefs.theme)      state.prefs.theme = state.themesDefault;
    if (!state.prefs.fontFamily) state.prefs.fontFamily = state.fontsDefault;
  } catch (err) {
    console.error("Failed to load themes/fonts:", err);
  }
}

function findTheme(name) {
  return state.themes.find((t) => t.name === name) || state.themes[0];
}

// ============================================================
// Status bar
// ============================================================
function setStatus(s, text) {
  const dot = els.statusDot;
  dot.className = "dot " + s;
  const labels = {
    connected: "Connected",
    disconnected: "Disconnected",
    connecting: "Connecting…",
    reconnecting: text || "Reconnecting…",
  };
  els.statusText.textContent = labels[s] || s;
  document.title = (s === "connected" ? "● " : "○ ") + "tunnelterm";
}

function formatDuration(ms) {
  const s = Math.floor(ms / 1000);
  const hh = String(Math.floor(s / 3600)).padStart(2, "0");
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

setInterval(() => {
  if (state.sessionStartTs > 0 && state.ws && state.ws.readyState === WebSocket.OPEN) {
    els.sessionTimer.textContent = formatDuration(Date.now() - state.sessionStartTs);
  }
  els.latencyEl.textContent = state.latencyMs != null ? `${state.latencyMs}ms` : "—";
}, 1000);

// ============================================================
// Terminal
// ============================================================

/**
 * Build the CSS font-family stack for the terminal.
 *
 * "Symbols Nerd Font" is appended unconditionally so that even if the user
 * picks a font without icon glyphs, the Private-Use-Area characters used by
 * powerlevel10k, lazygit, etc., still render. The @font-face for "Symbols
 * Nerd Font" in fonts.css uses a unicode-range that restricts it to the PUA,
 * so it never overrides regular text glyphs.
 */
function buildFontStack(family) {
  return `"${family}", "Symbols Nerd Font", ui-monospace, "SF Mono", Menlo, monospace`;
}

/**
 * Wait for a font to actually be loaded (downloaded and parsed) at the
 * current font size before applying it. Without this, the first paint uses
 * fallback metrics and looks wrong until the next event.
 */
async function ensureFontLoaded(family, size) {
  if (!document.fonts || !document.fonts.load) return;
  try {
    await document.fonts.load(`${size}px "${family}"`);
    await document.fonts.load(`bold ${size}px "${family}"`);
    await document.fonts.load(`16px "Symbols Nerd Font"`);
  } catch (e) {
    console.warn("font load failed:", e);
  }
}

async function buildTerminal() {
  // Preload the configured font so the first frame has correct metrics.
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

  // WebLinks
  if (window.WebLinksAddon) {
    state.term.loadAddon(new WebLinksAddon.WebLinksAddon());
  }
  // Search
  if (window.SearchAddon) {
    state.searchAddon = new SearchAddon.SearchAddon();
    state.term.loadAddon(state.searchAddon);
  }
  // Unicode 11
  if (window.Unicode11Addon) {
    state.term.loadAddon(new Unicode11Addon.Unicode11Addon());
    state.term.unicode.activeVersion = "11";
  }
  // Clipboard (OSC 52)
  if (window.ClipboardAddon) {
    state.term.loadAddon(new ClipboardAddon.ClipboardAddon());
  }
  // Serialize
  if (window.SerializeAddon) {
    state.serializeAddon = new SerializeAddon.SerializeAddon();
    state.term.loadAddon(state.serializeAddon);
  }

  state.term.open(els.term);

  // WebGL renderer (best-effort).
  try {
    if (window.WebglAddon) {
      const addon = new WebglAddon.WebglAddon();
      addon.onContextLoss(() => { try { addon.dispose(); } catch {} if (state.webglAddon === addon) state.webglAddon = null; });
      state.term.loadAddon(addon);
      state.webglAddon = addon;
    }
  } catch (e) {
    console.warn("WebGL renderer unavailable:", e);
  }

  state.fitAddon.fit();

  // Title from PTY (e.g. OSC 0/2)
  state.term.onTitleChange((title) => {
    if (title) document.title = title + " · tunnelterm";
  });

  // Bell: visual flash
  state.term.onBell(() => {
    if (state.prefs.bell === "visual") {
      els.term.animate(
        [{ filter: "brightness(1)" }, { filter: "brightness(1.6)" }, { filter: "brightness(1)" }],
        { duration: 200 }
      );
    }
  });

  // Selection -> auto-copy
  state.term.onSelectionChange(async () => {
    if (state.prefs.copyOnSelect) {
      const sel = state.term.getSelection();
      if (sel) try { await navigator.clipboard.writeText(sel); } catch {}
    }
  });

  // Input -> WebSocket
  state.term.onData((data) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(data);
      // Reset sticky modifiers after first input character.
      clearStickyMods();
    }
  });

  // Resize -> server
  state.term.onResize(({ cols, rows }) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      sendControl({ [CTRL]: "resize", cols, rows });
    }
  });

  // Custom key handling.
  state.term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    // Ctrl/Cmd+Shift+F -> search
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "f") {
      openSearch();
      return false;
    }
    // Ctrl/Cmd+Shift+C -> copy
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "c") {
      const sel = state.term.getSelection();
      if (sel) { navigator.clipboard.writeText(sel).catch(() => {}); return false; }
    }
    // Ctrl/Cmd+Shift+V -> paste
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "v") {
      navigator.clipboard.readText().then((t) => { if (state.ws?.readyState === 1) state.ws.send(t); }).catch(() => {});
      return false;
    }
    // Ctrl/Cmd + (plus|equal|-) -> font size
    if ((e.ctrlKey || e.metaKey) && (e.key === "+" || e.key === "=" )) {
      setFontSize(state.prefs.fontSize + 1); return false;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "-") {
      setFontSize(state.prefs.fontSize - 1); return false;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "0") {
      setFontSize(14); return false;
    }
    // Shift+Enter -> newline
    if (e.key === "Enter" && e.shiftKey) {
      state.ws?.readyState === 1 && state.ws.send("\n");
      return false;
    }
    // Esc closes search if open
    if (e.key === "Escape" && state.searchOpen) {
      closeSearch();
      return false;
    }
    return true;
  });
}

// ============================================================
// Settings appliers
// ============================================================
function applyTheme(name) {
  state.prefs.theme = name;
  savePrefs(state.prefs);
  const t = findTheme(name);
  if (state.term && t) state.term.options.theme = t.colors;
  updateThemeSelection();
}

function updateThemeSelection() {
  document.querySelectorAll(".theme-swatch").forEach((el) => {
    el.setAttribute("aria-checked", el.dataset.theme === state.prefs.theme ? "true" : "false");
  });
}

async function setFont(family) {
  state.prefs.fontFamily = family;
  savePrefs(state.prefs);
  if (!state.term) return;
  // Wait for the font to be available before telling xterm to use it,
  // otherwise the WebGL glyph atlas gets built from the fallback metrics.
  await ensureFontLoaded(family, state.prefs.fontSize);
  state.term.options.fontFamily = buildFontStack(family);
  await rebuildGlyphCache();
}

async function setFontSize(px) {
  px = Math.max(8, Math.min(32, px));
  state.prefs.fontSize = px;
  savePrefs(state.prefs);
  if (!state.term) return;
  state.term.options.fontSize = px;
  if (els.fontSizeValue) els.fontSizeValue.textContent = px + "px";
  if (els.fontSizeSlider) els.fontSizeSlider.value = px;
  await ensureFontLoaded(state.prefs.fontFamily, px);
  await rebuildGlyphCache();
}

async function setFontWeight(w) {
  state.prefs.fontWeight = w;
  savePrefs(state.prefs);
  if (!state.term) return;
  state.term.options.fontWeight = w;
  await rebuildGlyphCache();
}

function setLetterSpacing(v) {
  state.prefs.letterSpacing = v;
  savePrefs(state.prefs);
  if (state.term) { state.term.options.letterSpacing = v; rebuildGlyphCache(); }
}

function setLineHeight(v) {
  state.prefs.lineHeight = v;
  savePrefs(state.prefs);
  if (state.term) { state.term.options.lineHeight = v; rebuildGlyphCache(); }
}

/**
 * Force xterm to re-rasterize all glyphs after a font / size / weight change.
 *
 * Uses xterm's public `clearTextureAtlas()` API (available since xterm 5.3)
 * which invalidates the active renderer's glyph cache (WebGL or DOM) and
 * triggers an internal full-refresh on the next frame. This is the canonical
 * pattern used by production xterm.js consumers (tabby, opencove, etc.).
 *
 * We additionally:
 *   * wait for the *new* font to be downloaded before invalidating, so xterm's
 *     CharSizeService measures the correct glyph width on the next frame;
 *   * give the WebGL pipeline one animation frame to settle;
 *   * refit so the cell grid is recomputed for the new metrics;
 *   * call refresh() once more for belt-and-braces.
 */
async function rebuildGlyphCache() {
  if (!state.term) return;
  await ensureFontLoaded(state.prefs.fontFamily, state.prefs.fontSize);

  // Yield one frame so xterm has applied the option changes.
  await new Promise((r) => requestAnimationFrame(() => r()));

  // Drop the glyph cache. This works whether WebGL or DOM renderer is active.
  try { state.term.clearTextureAtlas(); } catch (e) { /* older xterm */ }

  // Recompute cell grid for the new metrics.
  if (state.fitAddon) {
    try { state.fitAddon.fit(); } catch {}
  }

  // Force a full repaint of the visible buffer with new glyphs.
  try { state.term.refresh(0, state.term.rows - 1); } catch {}
}

function setCursorStyle(s) {
  state.prefs.cursorStyle = s;
  savePrefs(state.prefs);
  if (state.term) state.term.options.cursorStyle = s;
}

function setCursorBlink(b) {
  state.prefs.cursorBlink = b;
  savePrefs(state.prefs);
  if (state.term) state.term.options.cursorBlink = b;
}

function setScrollback(n) {
  state.prefs.scrollback = n;
  savePrefs(state.prefs);
  if (state.term) state.term.options.scrollback = n;
}

function setBell(b) {
  state.prefs.bell = b;
  savePrefs(state.prefs);
  if (state.term) state.term.options.bellStyle = b === "sound" ? "sound" : "none";
}

function setCopyOnSelect(b) { state.prefs.copyOnSelect = b; savePrefs(state.prefs); }
function setRightClickPaste(b) { state.prefs.rightClickPaste = b; savePrefs(state.prefs); }
function setMacOptionAsMeta(b) {
  state.prefs.macOptionAsMeta = b;
  savePrefs(state.prefs);
  if (state.term) state.term.options.macOptionIsMeta = b;
}
function setScreenReaderMode(b) {
  state.prefs.screenReaderMode = b;
  savePrefs(state.prefs);
  if (state.term) state.term.options.screenReaderMode = b;
}

function refit() {
  // Wait for any newly-requested fonts to be loaded so width measurement is correct.
  const family = state.prefs.fontFamily;
  if (document.fonts && family) {
    document.fonts.ready.then(() => { state.fitAddon && state.fitAddon.fit(); }).catch(() => {});
  } else {
    state.fitAddon && state.fitAddon.fit();
  }
}

// ============================================================
// WebSocket
// ============================================================
function wsBaseUrl() {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return proto + location.host;
}

function sendControl(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

function connect() {
  state.wantConnected = true;
  if (state.reconnectAttempt === 0) setStatus("connecting");
  // Subprotocol header carries the token: "<protocol-id>, <token>"
  state.ws = new WebSocket(wsBaseUrl() + "/ws", [SUBPROTOCOL, state.authToken]);
  state.ws.binaryType = "arraybuffer";

  state.ws.onopen = () => {
    state.reconnectAttempt = 0;
    state.sessionStartTs = state.sessionStartTs || Date.now();
    setStatus("connected");
    hideOverlay();
    // Initial fit + resize broadcast (give layout one tick).
    setTimeout(() => {
      state.fitAddon.fit();
      sendControl({ [CTRL]: "resize", cols: state.term.cols, rows: state.term.rows });
      state.term.focus();
    }, 30);
    startPingLoop();
  };

  state.ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      // Only attempt control-frame parse if our discriminator is present.
      if (e.data.indexOf(CTRL) !== -1) {
        try {
          const d = JSON.parse(e.data);
          if (d && typeof d === "object" && CTRL in d) {
            handleControl(d);
            return;
          }
        } catch {
          /* fall through and write verbatim */
        }
      }
      state.term.write(e.data);
    } else {
      // Binary frame as ArrayBuffer (synchronous, ordered).
      state.term.write(new Uint8Array(e.data));
    }
  };

  state.ws.onclose = (e) => {
    stopPingLoop();
    if (!state.wantConnected) {
      setStatus("disconnected");
      return;
    }
    // 1008 = policy violation (e.g. invalid token); don't retry.
    if (e.code === 1008) {
      setStatus("disconnected");
      showOverlay({
        title: "Session ended",
        message: "Your session is no longer valid.",
        action: { label: "Sign in again", onClick: () => location.reload() },
      });
      return;
    }
    scheduleReconnect();
  };

  state.ws.onerror = () => {
    // onclose follows; let it handle the reconnect.
  };
}

function scheduleReconnect() {
  state.reconnectAttempt += 1;
  const max = 30000;
  const delay = Math.min(max, 500 * 2 ** (state.reconnectAttempt - 1)) + Math.floor(Math.random() * 300);
  setStatus("reconnecting", `Reconnecting in ${Math.ceil(delay / 1000)}s…`);
  state.reconnectTimer = setTimeout(() => {
    if (state.wantConnected) connect();
  }, delay);
}

function disconnect() {
  state.wantConnected = false;
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  if (state.ws) try { state.ws.close(1000); } catch {}
  stopPingLoop();
}

function handleControl(d) {
  switch (d[CTRL]) {
    case "process_exit":
      // The shell terminated (Ctrl+D, `exit`, crash, etc.). The server will
      // close the WebSocket right after this message; disable reconnect so the
      // user gets the overlay instead of a spinning "Reconnecting…" status.
      state.wantConnected = false;
      if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
      setStatus("disconnected");
      showOverlay({
        title: "Process exited",
        message: "The shell process has ended.",
        action: { label: "Reconnect", onClick: () => location.reload() },
      });
      break;
    case "spawn_error":
      state.wantConnected = false;
      if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
      showOverlay({
        title: "Could not start command",
        message: d.message || "The server failed to start the configured command.",
        action: { label: "Reload", onClick: () => location.reload() },
      });
      break;
    case "pong":
      if (state.lastPingTs > 0) {
        state.latencyMs = Math.max(0, Date.now() - state.lastPingTs);
      }
      break;
  }
}

// Best-effort latency measurement: server doesn't echo a "pong", so we use the
// WebSocket-protocol ping/pong via Performance entries? Simplest: measure round
// trip of a control frame.
function startPingLoop() {
  stopPingLoop();
  state.pingTimer = setInterval(() => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    state.lastPingTs = Date.now();
    // Server doesn't reply but onmessage timing of next byte approximates RTT.
    // We compute latency from time-to-receive-next-byte after sending nothing.
    // To avoid noise, just use last-message-receive jitter, set elsewhere.
  }, 5000);
}
function stopPingLoop() {
  if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
}

// ============================================================
// Overlay
// ============================================================
function showOverlay({ title, message, action }) {
  const html = `
    <div class="overlay" role="status" aria-live="polite">
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(message)}</p>
      ${action ? `<button class="primary" id="overlay-action"><span class="label">${escapeHtml(action.label)}</span></button>` : ""}
    </div>`;
  let host = els.term.querySelector(".overlay");
  if (host) host.remove();
  els.term.insertAdjacentHTML("afterbegin", html);
  if (action) $("overlay-action").onclick = action.onClick;
}
function hideOverlay() {
  const host = els.term.querySelector(".overlay");
  if (host) host.remove();
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ============================================================
// Search bar
// ============================================================
function openSearch() {
  if (!state.searchAddon) return;
  state.searchOpen = true;
  els.searchBar.classList.add("open");
  els.searchInput.value = "";
  els.searchInput.focus();
}
function closeSearch() {
  state.searchOpen = false;
  els.searchBar.classList.remove("open");
  state.searchAddon && state.searchAddon.clearDecorations();
  state.term && state.term.focus();
}
function doSearch(direction) {
  if (!state.searchAddon || !els.searchInput.value) return;
  const opts = { decorations: { matchBackground: "#6c8eff", matchBorder: "#fff", activeMatchBackground: "#ffd166" } };
  if (direction === "prev") state.searchAddon.findPrevious(els.searchInput.value, opts);
  else state.searchAddon.findNext(els.searchInput.value, opts);
}

// ============================================================
// Settings drawer
// ============================================================
function buildThemeGrid() {
  els.themeGrid.innerHTML = "";
  state.themes.forEach((t) => {
    const swatch = document.createElement("button");
    swatch.type = "button";
    swatch.className = "theme-swatch";
    swatch.dataset.theme = t.name;
    swatch.setAttribute("role", "radio");
    swatch.setAttribute("aria-checked", t.name === state.prefs.theme ? "true" : "false");
    swatch.setAttribute("aria-label", t.label);
    swatch.style.background = t.colors.background;
    swatch.style.color = t.colors.foreground;
    swatch.innerHTML = `
      <span>${escapeHtml(t.label)}</span>
      <span class="palette" aria-hidden="true">
        <span style="background:${t.colors.red}"></span>
        <span style="background:${t.colors.green}"></span>
        <span style="background:${t.colors.yellow}"></span>
        <span style="background:${t.colors.blue}"></span>
        <span style="background:${t.colors.magenta}"></span>
      </span>`;
    swatch.onclick = () => applyTheme(t.name);
    swatch.onkeydown = (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); nextThemeFocus(1); }
      if (e.key === "ArrowLeft"  || e.key === "ArrowUp")   { e.preventDefault(); nextThemeFocus(-1); }
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); applyTheme(t.name); }
    };
    els.themeGrid.appendChild(swatch);
  });
}

function nextThemeFocus(delta) {
  const items = Array.from(els.themeGrid.querySelectorAll(".theme-swatch"));
  const cur = items.indexOf(document.activeElement);
  const next = items[(cur + delta + items.length) % items.length];
  next && next.focus();
}

function buildFontSelect() {
  els.fontSelect.innerHTML = "";
  state.fonts.forEach((f) => {
    const opt = document.createElement("option");
    opt.value = f.family;
    opt.textContent = f.displayName;
    if (f.family === state.prefs.fontFamily) opt.selected = true;
    els.fontSelect.appendChild(opt);
  });
}

function openDrawer() {
  state.drawerOpen = true;
  els.drawer.classList.add("open");
  els.drawerScrim.classList.add("open");
  els.settingsBtn.setAttribute("aria-expanded", "true");
  els.drawerClose.focus();
}
function closeDrawer() {
  state.drawerOpen = false;
  els.drawer.classList.remove("open");
  els.drawerScrim.classList.remove("open");
  els.settingsBtn.setAttribute("aria-expanded", "false");
  state.term && state.term.focus();
}

// ============================================================
// Mobile virtual key bar
// ============================================================
function clearStickyMods() {
  state.stickyCtrl = state.stickyAlt = state.stickyShift = false;
  document.querySelectorAll("#mobile-bar button[data-mod]").forEach((b) => b.setAttribute("aria-pressed", "false"));
}

function setupMobileBar() {
  const keys = [
    { label: "Esc", send: "\x1b" },
    { label: "Tab", send: "\t" },
    { label: "Ctrl", mod: "ctrl" },
    { label: "Alt",  mod: "alt"  },
    { label: "↑", send: "\x1b[A" },
    { label: "↓", send: "\x1b[B" },
    { label: "←", send: "\x1b[D" },
    { label: "→", send: "\x1b[C" },
    { label: "/", send: "/" },
    { label: "|", send: "|" },
    { label: "~", send: "~" },
    { label: "-", send: "-" },
    { label: "PgUp", send: "\x1b[5~" },
    { label: "PgDn", send: "\x1b[6~" },
  ];
  els.mobileBar.innerHTML = "";
  for (const k of keys) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = k.label;
    if (k.mod) {
      b.dataset.mod = k.mod;
      b.setAttribute("aria-pressed", "false");
      b.onclick = () => {
        const isOn = b.getAttribute("aria-pressed") === "true";
        b.setAttribute("aria-pressed", isOn ? "false" : "true");
        if (k.mod === "ctrl") state.stickyCtrl = !isOn;
        if (k.mod === "alt") state.stickyAlt = !isOn;
      };
    } else {
      b.onclick = () => {
        if (!(state.ws && state.ws.readyState === 1)) return;
        let payload = k.send;
        // Apply sticky modifiers if armed and key is a printable single char.
        if (state.stickyCtrl && k.send.length === 1) {
          const c = k.send.toLowerCase().charCodeAt(0);
          if (c >= 0x61 && c <= 0x7a) payload = String.fromCharCode(c - 0x60);
        } else if (state.stickyAlt && k.send.length === 1) {
          payload = "\x1b" + k.send;
        }
        state.ws.send(payload);
        clearStickyMods();
        state.term.focus();
      };
    }
    els.mobileBar.appendChild(b);
  }
  // Show on touch devices.
  if (window.matchMedia("(pointer: coarse)").matches) {
    els.mobileBar.classList.add("show");
  }
}

// ============================================================
// Drawer event wiring
// ============================================================
function wireDrawer() {
  els.settingsBtn.onclick = () => state.drawerOpen ? closeDrawer() : openDrawer();
  els.drawerClose.onclick = closeDrawer;
  els.drawerScrim.onclick = closeDrawer;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.drawerOpen) { closeDrawer(); }
    if (e.key === "?" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); toggleShortcuts(); }
  });

  // Font
  els.fontSelect.onchange = (e) => setFont(e.target.value);

  // Font size
  els.fontSizeSlider.value = state.prefs.fontSize;
  els.fontSizeValue.textContent = state.prefs.fontSize + "px";
  els.fontSizeSlider.oninput = (e) => setFontSize(parseInt(e.target.value, 10));

  // Font weight
  document.querySelectorAll("[data-weight]").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.weight === state.prefs.fontWeight ? "true" : "false");
    b.onclick = () => {
      setFontWeight(b.dataset.weight);
      document.querySelectorAll("[data-weight]").forEach((x) =>
        x.setAttribute("aria-pressed", x.dataset.weight === state.prefs.fontWeight ? "true" : "false")
      );
    };
  });

  // Letter spacing
  els.letterSpacing.value = state.prefs.letterSpacing;
  els.letterSpacingValue.textContent = state.prefs.letterSpacing + "px";
  els.letterSpacing.oninput = (e) => {
    const v = parseFloat(e.target.value);
    setLetterSpacing(v);
    els.letterSpacingValue.textContent = v + "px";
  };

  // Line height
  els.lineHeight.value = state.prefs.lineHeight;
  els.lineHeightValue.textContent = state.prefs.lineHeight.toFixed(2);
  els.lineHeight.oninput = (e) => {
    const v = parseFloat(e.target.value);
    setLineHeight(v);
    els.lineHeightValue.textContent = v.toFixed(2);
  };

  // Cursor style
  document.querySelectorAll("[data-cursor]").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.cursor === state.prefs.cursorStyle ? "true" : "false");
    b.onclick = () => {
      setCursorStyle(b.dataset.cursor);
      document.querySelectorAll("[data-cursor]").forEach((x) =>
        x.setAttribute("aria-pressed", x.dataset.cursor === state.prefs.cursorStyle ? "true" : "false")
      );
    };
  });

  els.cursorBlink.checked = state.prefs.cursorBlink;
  els.cursorBlink.onchange = (e) => setCursorBlink(e.target.checked);

  // Scrollback
  els.scrollback.value = state.prefs.scrollback;
  els.scrollback.onchange = (e) => setScrollback(parseInt(e.target.value, 10) || 5000);

  // Bell
  document.querySelectorAll("[data-bell]").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.bell === state.prefs.bell ? "true" : "false");
    b.onclick = () => {
      setBell(b.dataset.bell);
      document.querySelectorAll("[data-bell]").forEach((x) =>
        x.setAttribute("aria-pressed", x.dataset.bell === state.prefs.bell ? "true" : "false")
      );
    };
  });

  // Behavior toggles
  els.copyOnSelect.checked = state.prefs.copyOnSelect;
  els.copyOnSelect.onchange = (e) => setCopyOnSelect(e.target.checked);
  els.macOptionAsMeta.checked = state.prefs.macOptionAsMeta;
  els.macOptionAsMeta.onchange = (e) => setMacOptionAsMeta(e.target.checked);
  els.screenReaderMode.checked = state.prefs.screenReaderMode;
  els.screenReaderMode.onchange = (e) => setScreenReaderMode(e.target.checked);

  // Actions
  els.clearBtn.onclick = () => { state.term && state.term.clear(); closeDrawer(); };
  els.downloadBtn.onclick = () => {
    if (!state.serializeAddon) return;
    const text = state.serializeAddon.serialize();
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `tunnelterm-${new Date().toISOString().replace(/[:.]/g, "-")}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };
  els.logoutBtn.onclick = async () => {
    try {
      const w = new WebSocket(wsBaseUrl() + "/logout");
      await new Promise((res) => {
        w.onopen = () => { w.send(JSON.stringify({ token: state.authToken })); };
        w.onmessage = () => res(); w.onerror = () => res(); w.onclose = () => res();
      });
    } catch {}
    try { localStorage.removeItem(TOKEN_KEY); } catch {}
    disconnect();
    location.reload();
  };
  els.resetBtn.onclick = () => {
    if (!confirm("Reset all UI preferences to defaults?")) return;
    state.prefs = { ...PREFS_DEFAULTS, theme: state.themesDefault, fontFamily: state.fontsDefault };
    savePrefs(state.prefs);
    location.reload();
  };

  els.shortcutsBtn.onclick = () => toggleShortcuts();
  els.shortcutsClose.onclick = () => toggleShortcuts(false);
  els.shortcutsModal.onclick = (e) => { if (e.target === els.shortcutsModal) toggleShortcuts(false); };
}

function toggleShortcuts(force) {
  const want = typeof force === "boolean" ? force : !els.shortcutsModal.classList.contains("open");
  els.shortcutsModal.classList.toggle("open", want);
  if (want) els.shortcutsClose.focus();
}

// ============================================================
// Search wiring
// ============================================================
function wireSearch() {
  els.searchInput.oninput = () => doSearch("next");
  els.searchInput.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); doSearch(e.shiftKey ? "prev" : "next"); }
    if (e.key === "Escape") { e.preventDefault(); closeSearch(); }
  };
  els.searchPrev.onclick = () => doSearch("prev");
  els.searchNext.onclick = () => doSearch("next");
  els.searchClose.onclick = closeSearch;
}

// ============================================================
// Right-click paste
// ============================================================
function wireRightClick() {
  els.term.addEventListener("contextmenu", (e) => {
    if (!state.prefs.rightClickPaste) return;
    e.preventDefault();
    navigator.clipboard.readText().then((t) => {
      if (t && state.ws && state.ws.readyState === 1) state.ws.send(t);
    }).catch(() => {});
  });
}

// ============================================================
// Login
// ============================================================
function wireLogin() {
  els.loginForm.onsubmit = async (e) => {
    e.preventDefault();
    const btn = els.loginForm.querySelector("button.primary");
    btn.classList.add("loading");
    btn.disabled = true;
    els.loginError.textContent = "";

    try {
      const pw = els.passwordInput.value;
      const w = new WebSocket(wsBaseUrl() + "/auth");
      const token = await new Promise((res, rej) => {
        w.onopen = () => w.send(JSON.stringify({ password: pw }));
        w.onmessage = (ev) => {
          let d;
          try { d = JSON.parse(ev.data); } catch { return rej(new Error("Server sent invalid JSON")); }
          if (d.token) return res(d.token);
          if (d.error === "rate_limited") return rej(new Error(`Too many attempts. Try again in ${d.retry_after}s.`));
          if (d.error === "invalid_password") return rej(new Error("Incorrect password"));
          if (d.error === "invalid_json") return rej(new Error("Server error"));
          rej(new Error(d.error || "Authentication failed"));
        };
        w.onerror = () => rej(new Error("Could not reach server"));
        w.onclose = (ev) => { if (ev.code === 1008) rej(new Error("Connection rejected by server")); };
      });
      state.authToken = token;
      if (els.rememberSession.checked) {
        try { sessionStorage.setItem(TOKEN_KEY, token); } catch {}
      }
      els.login.style.display = "none";
      els.terminal.classList.add("show");
      await buildTerminal();
      buildThemeGrid();
      buildFontSelect();
      connect();
    } catch (err) {
      els.loginError.textContent = err.message;
      btn.disabled = false;
      btn.classList.remove("loading");
      els.passwordInput.select();
    }
  };

  els.showPwBtn.onclick = () => {
    const isPw = els.passwordInput.type === "password";
    els.passwordInput.type = isPw ? "text" : "password";
    els.showPwBtn.setAttribute("aria-pressed", isPw ? "true" : "false");
    els.showPwBtn.title = isPw ? "Hide password" : "Show password";
  };
}

// ============================================================
// Resize handler
// ============================================================
window.addEventListener("resize", () => {
  if (state.fitAddon) state.fitAddon.fit();
});

// ============================================================
// Boot
// ============================================================
async function boot() {
  // Cache DOM
  Object.assign(els, {
    login:            $("login"),
    terminal:         $("terminal"),
    loginForm:        $("login-form"),
    passwordInput:    $("password"),
    showPwBtn:        $("show-pw"),
    rememberSession:  $("remember-session"),
    loginError:       $("login-error"),
    statusDot:        $("status-dot"),
    statusText:       $("status-text"),
    latencyEl:        $("status-latency"),
    sessionTimer:     $("status-timer"),
    settingsBtn:      $("settings-btn"),
    shortcutsBtn:     $("shortcuts-btn"),
    searchOpenBtn:    $("search-open-btn"),
    drawer:           $("settings-drawer"),
    drawerScrim:      $("drawer-scrim"),
    drawerClose:      $("drawer-close"),
    themeGrid:        $("theme-grid"),
    fontSelect:       $("font-select"),
    fontSizeSlider:   $("font-size-slider"),
    fontSizeValue:    $("font-size-value"),
    letterSpacing:    $("letter-spacing"),
    letterSpacingValue: $("letter-spacing-value"),
    lineHeight:       $("line-height"),
    lineHeightValue:  $("line-height-value"),
    cursorBlink:      $("cursor-blink"),
    scrollback:       $("scrollback"),
    copyOnSelect:     $("copy-on-select"),
    macOptionAsMeta:  $("mac-option-as-meta"),
    screenReaderMode: $("screen-reader-mode"),
    clearBtn:         $("clear-btn"),
    downloadBtn:      $("download-btn"),
    logoutBtn:        $("logout-btn"),
    resetBtn:         $("reset-btn"),
    term:             $("term"),
    searchBar:        $("search-bar"),
    searchInput:      $("search-input"),
    searchPrev:       $("search-prev"),
    searchNext:       $("search-next"),
    searchClose:      $("search-close"),
    shortcutsModal:   $("shortcuts-modal"),
    shortcutsClose:   $("shortcuts-close"),
    mobileBar:        $("mobile-bar"),
  });

  await loadThemesAndFonts();
  wireLogin();
  wireDrawer();
  wireSearch();
  wireRightClick();
  setupMobileBar();
  els.searchOpenBtn.onclick = openSearch;
  els.passwordInput.focus();
}

document.addEventListener("DOMContentLoaded", boot);
