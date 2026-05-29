/* Mobile virtual key-bar (Esc/Tab/Ctrl/Alt/arrows/etc.).
 *
 * Ctrl and Alt are sticky: tap to arm, tap any non-modifier key and they
 * apply once, then auto-clear. We also clear them on the next physical
 * keypress from xterm.onData. */
"use strict";

import { els, state } from "../state.js";

export function clearStickyMods() {
  state.stickyCtrl = state.stickyAlt = state.stickyShift = false;
  document
    .querySelectorAll("#mobile-bar button[data-mod]")
    .forEach((b) => b.setAttribute("aria-pressed", "false"));
}

export function setupMobileBar() {
  const keys = [
    { label: "Esc", send: "\x1b" },
    { label: "Tab", send: "\t" },
    { label: "Ctrl", mod: "ctrl" },
    { label: "Alt", mod: "alt" },
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
  if (window.matchMedia("(pointer: coarse)").matches) {
    els.mobileBar.classList.add("show");
  }
}
