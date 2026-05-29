/* WebSocket client.
 *
 * The session cookie is set by /api/auth and rides the WS handshake
 * automatically; this module does NOT see the token. */
"use strict";

import { wsUrl } from "./api.js";
import { CTRL } from "./constants.js";
import { state } from "./state.js";
import { setStatus } from "./status.js";
import { hideOverlay, showOverlay } from "./ui/overlay.js";

export function sendControl(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

export function connect() {
  state.wantConnected = true;
  if (state.reconnectAttempt === 0) setStatus("connecting");
  // No subprotocols, no headers; cookie auth handles identity.
  state.ws = new WebSocket(wsUrl("/ws"));
  state.ws.binaryType = "arraybuffer";

  state.ws.onopen = () => {
    state.reconnectAttempt = 0;
    state.sessionStartTs = state.sessionStartTs || Date.now();
    setStatus("connected");
    hideOverlay();
    setTimeout(() => {
      state.fitAddon.fit();
      sendControl({ [CTRL]: "resize", cols: state.term.cols, rows: state.term.rows });
      state.term.focus();
    }, 30);
    startPingLoop();
  };

  state.ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      if (e.data.indexOf(CTRL) !== -1) {
        try {
          const d = JSON.parse(e.data);
          if (d && typeof d === "object" && CTRL in d) {
            handleControl(d);
            return;
          }
        } catch { /* fall through */ }
      }
      state.term.write(e.data);
    } else {
      state.term.write(new Uint8Array(e.data));
    }
  };

  state.ws.onclose = (e) => {
    stopPingLoop();
    if (!state.wantConnected) {
      setStatus("disconnected");
      return;
    }
    if (e.code === 1008) {
      // Policy violation: bad / missing / revoked cookie. No point in retrying.
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

  state.ws.onerror = () => { /* onclose runs right after; nothing to do here */ };
}

function scheduleReconnect() {
  state.reconnectAttempt += 1;
  const max = 30000;
  const delay =
    Math.min(max, 500 * 2 ** (state.reconnectAttempt - 1)) +
    Math.floor(Math.random() * 300);
  setStatus("reconnecting", `Reconnecting in ${Math.ceil(delay / 1000)}s…`);
  state.reconnectTimer = setTimeout(() => {
    if (state.wantConnected) connect();
  }, delay);
}

export function disconnect() {
  state.wantConnected = false;
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  if (state.ws) try { state.ws.close(1000); } catch { /* swallow */ }
  stopPingLoop();
}

function handleControl(d) {
  switch (d[CTRL]) {
    case "process_exit":
      // Server closes the WS right after this; suppress reconnect.
      state.wantConnected = false;
      if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
      }
      setStatus("disconnected");
      showOverlay({
        title: "Process exited",
        message: "The shell process has ended.",
        action: { label: "Reconnect", onClick: () => location.reload() },
      });
      break;
    case "spawn_error":
      state.wantConnected = false;
      if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
      }
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

function startPingLoop() {
  stopPingLoop();
  state.pingTimer = setInterval(() => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    state.lastPingTs = Date.now();
  }, 5000);
}

function stopPingLoop() {
  if (state.pingTimer) {
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }
}
