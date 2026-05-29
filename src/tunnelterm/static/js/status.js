/* Status bar: connection dot, latency, session timer. */
"use strict";

import { els, state } from "./state.js";

export function setStatus(s, text) {
  els.statusDot.className = "dot " + s;
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

/** Start the once-per-second timer/latency display. Idempotent. */
let _started = false;
export function startStatusClock() {
  if (_started) return;
  _started = true;
  setInterval(() => {
    if (state.sessionStartTs > 0 && state.ws && state.ws.readyState === WebSocket.OPEN) {
      els.sessionTimer.textContent = formatDuration(Date.now() - state.sessionStartTs);
    }
    els.latencyEl.textContent = state.latencyMs != null ? `${state.latencyMs}ms` : "—";
  }, 1000);
}
