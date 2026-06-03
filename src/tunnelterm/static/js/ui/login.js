/* Login form + auto-login (cookie verify) flow. */
"use strict";

import { authMode, login as apiLogin } from "../api.js";
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

/** Show the TOTP field + hint. Idempotent. */
function showTotpField() {
  if (!els.totpRow || els.totpRow.hidden) {
    if (els.totpRow) els.totpRow.hidden = false;
    if (els.totpHint) els.totpHint.hidden = false;
    // Re-trigger the reveal animation by toggling the row off-then-on
    // across a frame; otherwise an already-rendered row won't animate.
    if (els.totpRow) {
      els.totpRow.style.animation = "none";
      void els.totpRow.offsetWidth;
      els.totpRow.style.animation = "";
    }
  }
  // Move focus to the TOTP field so authenticator-app auto-fill lands here.
  if (els.totpInput) els.totpInput.focus();
}

/** Read the TOTP code from the input, or null if the field is hidden/empty. */
function readTotp() {
  if (!els.totpInput || !els.totpRow || els.totpRow.hidden) return null;
  const v = els.totpInput.value.replace(/\s+/g, "");
  return v.length > 0 ? v : null;
}

/**
 * Wire up the login form. The TOTP field is shown only when the server
 * reports ``require_totp = true``; if the probe fails we degrade to the
 * password-only form and reveal the TOTP field after the first failed
 * attempt (in case the user is on a deployment that does require it).
 */
export async function wireLogin() {
  const mode = await authMode();
  if (mode.require_totp) showTotpField();

  els.loginForm.onsubmit = async (e) => {
    e.preventDefault();
    const btn = els.loginForm.querySelector("button.primary");
    btn.classList.add("loading");
    btn.disabled = true;
    els.loginError.textContent = "";

    const password = els.passwordInput.value;
    const totp = readTotp();

    try {
      await apiLogin(password, totp);
      // Cookie is now set by the server; just enter the terminal.
      await enterTerminal();
    } catch (err) {
      els.loginError.textContent = err.message;
      btn.disabled = false;
      btn.classList.remove("loading");
      // If we got a generic "Incorrect password or code" and the TOTP
      // field is still hidden, the server probably requires TOTP. Reveal
      // it so the user can add the code on the next attempt.
      const needTotp = /code|password or code/i.test(err.message);
      if (needTotp && (!els.totpRow || els.totpRow.hidden)) {
        showTotpField();
      }
      // Focus the field that most likely needs the next keystroke.
      if (els.totpRow && !els.totpRow.hidden && els.totpInput && !els.totpInput.value) {
        els.totpInput.focus();
      } else {
        els.passwordInput.select();
      }
    }
  };

  els.showPwBtn.onclick = () => {
    const isPw = els.passwordInput.type === "password";
    els.passwordInput.type = isPw ? "text" : "password";
    els.showPwBtn.setAttribute("aria-pressed", isPw ? "true" : "false");
    els.showPwBtn.title = isPw ? "Hide password" : "Show password";
  };

  // Enter inside the TOTP field submits the form (default behavior of
  // form submission handles this; we just make sure type!=submit doesn't
  // swallow it, which it doesn't for <input type="text">).
}
