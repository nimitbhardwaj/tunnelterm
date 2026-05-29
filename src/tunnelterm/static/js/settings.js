/* Settings drawer: applier functions + DOM wiring.
 *
 * The "applier" functions all mutate state.prefs, persist via savePrefs,
 * and (where applicable) push the change into xterm. */
"use strict";

import { logout as apiLogout } from "./api.js";
import { PREFS_DEFAULTS, SCROLLBACK_MAX } from "./constants.js";
import { els, savePrefs, state } from "./state.js";
import { buildFontStack, ensureFontLoaded, rebuildGlyphCache } from "./terminal.js";
import { findTheme } from "./themes.js";
import { escapeHtml } from "./ui/dom.js";

// ---------- appliers ----------

export function applyTheme(name) {
  state.prefs.theme = name;
  savePrefs(state.prefs);
  const t = findTheme(name);
  if (state.term && t) state.term.options.theme = t.colors;
  updateThemeSelection();
}

function updateThemeSelection() {
  document.querySelectorAll(".theme-swatch").forEach((el) => {
    el.setAttribute(
      "aria-checked",
      el.dataset.theme === state.prefs.theme ? "true" : "false",
    );
  });
}

export async function setFont(family) {
  state.prefs.fontFamily = family;
  savePrefs(state.prefs);
  if (!state.term) return;
  await ensureFontLoaded(family, state.prefs.fontSize);
  state.term.options.fontFamily = buildFontStack(family);
  await rebuildGlyphCache();
}

export async function setFontSize(px) {
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
  if (state.term) {
    state.term.options.letterSpacing = v;
    rebuildGlyphCache();
  }
}

function setLineHeight(v) {
  state.prefs.lineHeight = v;
  savePrefs(state.prefs);
  if (state.term) {
    state.term.options.lineHeight = v;
    rebuildGlyphCache();
  }
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
  const capped = Math.max(0, Math.min(SCROLLBACK_MAX, Number(n) || 5000));
  state.prefs.scrollback = capped;
  savePrefs(state.prefs);
  if (state.term) state.term.options.scrollback = capped;
}

function setBell(b) {
  state.prefs.bell = b;
  savePrefs(state.prefs);
  if (state.term) state.term.options.bellStyle = b === "sound" ? "sound" : "none";
}

function setCopyOnSelect(b) {
  state.prefs.copyOnSelect = b;
  savePrefs(state.prefs);
}

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

// ---------- theme grid + font select builders ----------

export function buildThemeGrid() {
  els.themeGrid.innerHTML = "";
  state.themes.forEach((t) => {
    const swatch = document.createElement("button");
    swatch.type = "button";
    swatch.className = "theme-swatch";
    swatch.dataset.theme = t.name;
    swatch.setAttribute("role", "radio");
    swatch.setAttribute(
      "aria-checked",
      t.name === state.prefs.theme ? "true" : "false",
    );
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
      if (e.key === "ArrowRight" || e.key === "ArrowDown") {
        e.preventDefault();
        nextThemeFocus(1);
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        nextThemeFocus(-1);
      }
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        applyTheme(t.name);
      }
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

export function buildFontSelect() {
  els.fontSelect.innerHTML = "";
  state.fonts.forEach((f) => {
    const opt = document.createElement("option");
    opt.value = f.family;
    opt.textContent = f.displayName;
    if (f.family === state.prefs.fontFamily) opt.selected = true;
    els.fontSelect.appendChild(opt);
  });
}

// ---------- drawer ----------

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

function toggleShortcuts(force) {
  const want =
    typeof force === "boolean" ? force : !els.shortcutsModal.classList.contains("open");
  els.shortcutsModal.classList.toggle("open", want);
  if (want) els.shortcutsClose.focus();
}

export function wireDrawer({ onLogout }) {
  els.settingsBtn.onclick = () => (state.drawerOpen ? closeDrawer() : openDrawer());
  els.drawerClose.onclick = closeDrawer;
  els.drawerScrim.onclick = closeDrawer;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.drawerOpen) closeDrawer();
    if (e.key === "?" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      toggleShortcuts();
    }
  });

  els.fontSelect.onchange = (e) => setFont(e.target.value);

  els.fontSizeSlider.value = state.prefs.fontSize;
  els.fontSizeValue.textContent = state.prefs.fontSize + "px";
  els.fontSizeSlider.oninput = (e) => setFontSize(parseInt(e.target.value, 10));

  document.querySelectorAll("[data-weight]").forEach((b) => {
    b.setAttribute(
      "aria-pressed",
      b.dataset.weight === state.prefs.fontWeight ? "true" : "false",
    );
    b.onclick = () => {
      setFontWeight(b.dataset.weight);
      document.querySelectorAll("[data-weight]").forEach((x) =>
        x.setAttribute(
          "aria-pressed",
          x.dataset.weight === state.prefs.fontWeight ? "true" : "false",
        ),
      );
    };
  });

  els.letterSpacing.value = state.prefs.letterSpacing;
  els.letterSpacingValue.textContent = state.prefs.letterSpacing + "px";
  els.letterSpacing.oninput = (e) => {
    const v = parseFloat(e.target.value);
    setLetterSpacing(v);
    els.letterSpacingValue.textContent = v + "px";
  };

  els.lineHeight.value = state.prefs.lineHeight;
  els.lineHeightValue.textContent = state.prefs.lineHeight.toFixed(2);
  els.lineHeight.oninput = (e) => {
    const v = parseFloat(e.target.value);
    setLineHeight(v);
    els.lineHeightValue.textContent = v.toFixed(2);
  };

  document.querySelectorAll("[data-cursor]").forEach((b) => {
    b.setAttribute(
      "aria-pressed",
      b.dataset.cursor === state.prefs.cursorStyle ? "true" : "false",
    );
    b.onclick = () => {
      setCursorStyle(b.dataset.cursor);
      document.querySelectorAll("[data-cursor]").forEach((x) =>
        x.setAttribute(
          "aria-pressed",
          x.dataset.cursor === state.prefs.cursorStyle ? "true" : "false",
        ),
      );
    };
  });

  els.cursorBlink.checked = state.prefs.cursorBlink;
  els.cursorBlink.onchange = (e) => setCursorBlink(e.target.checked);

  els.scrollback.value = state.prefs.scrollback;
  els.scrollback.onchange = (e) => setScrollback(parseInt(e.target.value, 10) || 5000);

  document.querySelectorAll("[data-bell]").forEach((b) => {
    b.setAttribute(
      "aria-pressed",
      b.dataset.bell === state.prefs.bell ? "true" : "false",
    );
    b.onclick = () => {
      setBell(b.dataset.bell);
      document.querySelectorAll("[data-bell]").forEach((x) =>
        x.setAttribute(
          "aria-pressed",
          x.dataset.bell === state.prefs.bell ? "true" : "false",
        ),
      );
    };
  });

  els.copyOnSelect.checked = state.prefs.copyOnSelect;
  els.copyOnSelect.onchange = (e) => setCopyOnSelect(e.target.checked);
  els.macOptionAsMeta.checked = state.prefs.macOptionAsMeta;
  els.macOptionAsMeta.onchange = (e) => setMacOptionAsMeta(e.target.checked);
  els.screenReaderMode.checked = state.prefs.screenReaderMode;
  els.screenReaderMode.onchange = (e) => setScreenReaderMode(e.target.checked);

  els.clearBtn.onclick = () => {
    state.term && state.term.clear();
    closeDrawer();
  };
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
    await apiLogout();
    if (onLogout) onLogout();
    location.reload();
  };

  els.resetBtn.onclick = () => {
    if (!confirm("Reset all UI preferences to defaults?")) return;
    state.prefs = {
      ...PREFS_DEFAULTS,
      theme: state.themesDefault,
      fontFamily: state.fontsDefault,
    };
    savePrefs(state.prefs);
    location.reload();
  };

  els.shortcutsBtn.onclick = () => toggleShortcuts();
  els.shortcutsClose.onclick = () => toggleShortcuts(false);
  els.shortcutsModal.onclick = (e) => {
    if (e.target === els.shortcutsModal) toggleShortcuts(false);
  };
}
