/* Modal-ish overlay displayed inside the terminal viewport for
 * disconnect / process-exit / spawn-error notifications. */
"use strict";

import { $, els } from "../state.js";
import { escapeHtml } from "./dom.js";

export function showOverlay({ title, message, action }) {
  const html = `
    <div class="overlay" role="status" aria-live="polite">
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(message)}</p>
      ${action ? `<button class="primary" id="overlay-action"><span class="label">${escapeHtml(action.label)}</span></button>` : ""}
    </div>`;
  const existing = els.term.querySelector(".overlay");
  if (existing) existing.remove();
  els.term.insertAdjacentHTML("afterbegin", html);
  if (action) {
    const btn = $("overlay-action");
    if (btn) btn.onclick = action.onClick;
  }
}

export function hideOverlay() {
  const host = els.term.querySelector(".overlay");
  if (host) host.remove();
}
