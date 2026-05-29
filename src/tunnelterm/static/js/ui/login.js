/* Login form + auto-login (cookie verify) flow. */
"use strict";

import { login as apiLogin } from "../api.js";
import { els, state } from "../state.js";
import { buildTerminal } from "../terminal.js";
import { buildFontSelect, buildThemeGrid, setFontSize, wireDrawer } from "../settings.js";
import { connect, disconnect } from "../ws.js";

/**
 * Swap from the login view to the terminal view. Builds xterm and wires
 * the drawer the first time; subsequent calls only re-open the WebSocket.
 */
export async function enterTerminal() {
  els.login.style.display = "none";
  els.terminal.classList.add("show");
  if (!state.terminalUiBuilt) {
    await buildTerminal({ onFontSizeNudge: (px) => setFontSize(px) });
    buildThemeGrid();
    buildFontSelect();
    wireDrawer({ onLogout: () => disconnect() });
    state.terminalUiBuilt = true;
  }
  connect();
}

export function wireLogin() {
  els.loginForm.onsubmit = async (e) => {
    e.preventDefault();
    const btn = els.loginForm.querySelector("button.primary");
    btn.classList.add("loading");
    btn.disabled = true;
    els.loginError.textContent = "";

    try {
      await apiLogin(els.passwordInput.value);
      // Cookie is now set by the server; just enter the terminal.
      await enterTerminal();
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
