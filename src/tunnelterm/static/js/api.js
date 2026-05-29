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

/**
 * Submit credentials. Returns `{ok}` on success or throws on failure with
 * a user-readable error message.
 */
export async function login(password) {
  const { status, data } = await postJson("/api/auth", { password });
  if (status === 200 && data && data.ok) return true;
  if (status === 429) {
    const retry = data && data.retry_after ? Math.ceil(data.retry_after) : 60;
    throw new Error(`Too many attempts. Try again in ${retry}s.`);
  }
  if (status === 401) throw new Error("Incorrect password");
  if (status === 403) throw new Error("Connection rejected (origin not allowed)");
  throw new Error((data && data.error) || `Login failed (HTTP ${status})`);
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
