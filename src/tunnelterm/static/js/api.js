/* HTTP API client.
 *
 * As of v0.1.4, auth flows are plain JSON-over-HTTP POSTs. The browser
 * carries the HttpOnly session cookie automatically; this module never
 * sees the token. All requests use `credentials: 'same-origin'` to make
 * cookie behavior explicit and survive future fetch() defaults changes.
 */
"use strict";

function wsBaseUrl() {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return proto + location.host;
}

export function wsUrl(path = "/ws") {
  return wsBaseUrl() + path;
}

async function postJson(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await resp.json(); } catch { /* may be empty */ }
  return { status: resp.status, data };
}

async function getJson(path) {
  const resp = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { "Accept": "application/json" },
  });
  let data = null;
  try { data = await resp.json(); } catch { /* may be empty */ }
  return { status: resp.status, data };
}

/**
 * Submit credentials. Returns `{ok}` on success or throws on failure with
 * a user-readable error message.
 *
 * ``totp`` is optional. When the server has TOTP enforced, it must be a
 * 6-digit string; when not enforced, the value is ignored (send ``null``).
 */
export async function login(password, totp = null) {
  const body = { password };
  if (totp != null && totp !== "") body.totp = totp;
  const { status, data } = await postJson("/api/auth", body);
  if (status === 200 && data && data.ok) return true;
  if (status === 429) {
    const retry = data && data.retry_after ? Math.ceil(data.retry_after) : 60;
    throw new Error(`Too many attempts. Try again in ${retry}s.`);
  }
  if (status === 401) {
    // Generic message: the server intentionally does not disclose which
    // factor (password vs. TOTP) was wrong, and we mirror that here.
    throw new Error("Incorrect password or code");
  }
  if (status === 403) throw new Error("Connection rejected (origin not allowed)");
  throw new Error((data && data.error) || `Login failed (HTTP ${status})`);
}

/**
 * Probe the current auth mode. Returns ``{require_totp}`` so the login
 * form can render the TOTP field conditionally. Falls back to
 * ``{require_totp: false}`` on any error -- the safe default is to show
 * only the password field; the user will get a 401 on submit and the
 * form will reveal the TOTP field on the next attempt.
 */
export async function authMode() {
  try {
    const { status, data } = await getJson("/api/auth/mode");
    if (status === 200 && data) return { require_totp: !!data.require_totp };
  } catch { /* swallow */ }
  return { require_totp: false };
}

/** Return true if the browser's session cookie still maps to a valid token. */
export async function verifySession() {
  try {
    const { status, data } = await postJson("/api/verify", {});
    return status === 200 && !!(data && data.ok);
  } catch {
    return false;
  }
}

/** Tell the server to revoke the cookie's token. Best-effort; never throws. */
export async function logout() {
  try { await postJson("/api/logout", {}); } catch { /* swallow */ }
}
