# Architecture

tunnelterm is a web-based terminal: a browser acts as a terminal emulator while the server spawns a real shell in a PTY and bridges bytes over a WebSocket.

## Request flow

```
Browser                    tunnelterm (FastAPI/uvicorn)
 ──────                    ─────────────────────────────

  GET /                    http_routes.py → serves index.html (SPA shell)
  GET /healthz
  GET /static/*

  POST /api/auth           auth_routes.py
    body: {password, totp?}  → verify password (and TOTP if --require-totp),
                              mint token, Set-Cookie (HttpOnly; Secure; SameSite=Strict)
    response: {ok: true}

  GET  /api/auth/mode       auth_routes.py
                              → {require_totp: bool}; login form probes this at
                                boot to decide whether to render the TOTP field

  POST /api/verify          auth_routes.py
    cookie: tt_session        → check token validity (rate-limited)

  POST /api/logout          auth_routes.py
    cookie: tt_session        → revoke token, discard sticky PTY session, clear cookie

  WS /ws                    terminal_ws.py
    Origin allow-list check (fail-closed on non-loopback binds)
    Cookie token validation
    Single-session enforcement (token may only have one active WS at a time)
    → bridge_session() runs for the lifetime of the connection
```

## Modules

### `__main__` / `main`
**`tunnelterm.__main__`** — CLI argument parsing, config loading, fail-closed origin policy, instantiation of the singleton `Authenticator`.

**`tunnelterm.main`** — `create_app()` builds the FastAPI app; `run()` launches uvicorn. Static assets (`/static`) are mounted here.

### `auth`
**`tunnelterm.auth`** — `Authenticator` holds the password, optional TOTP secret, token store (LRU, 64 max, 24h TTL), and per-IP rate limiters. Singleton, initialized once at startup. When `require_totp` is True, `/api/auth` demands a valid RFC 6238 TOTP code in addition to the password; `verify_totp()` uses a ±1 step (30 s) clock-skew window.

`TrustedProxies` resolves `X-Forwarded-For` / `X-Forwarded-Proto` from trusted CIDRs only (default: loopback), preventing spoofing.

Token fingerprints (SHA-256 prefix) are logged instead of raw tokens.

### `session` / `pty_manager`
**`tunnelterm.session`** — `PtySession` owns a PTY and persists it across WebSocket connect/disconnect cycles (sticky sessions). Holds a 1 MiB ring buffer of PTY output for replay on reconnect. Tracks attach/detach state for the idle reaper.

**`SessionRegistry`** — process-wide `token → PtySession` map with a background reaper that kills detached sessions after `idle_timeout` (default 5h). `get_or_create()` respawns the PTY if the shell exited while detached.

**`tunnelterm.pty_manager`** — `PtyManager` wraps `pty.fork()`. Sets `TERM=xterm-256color`. Uses an exec-error pipe so parent learns synchronously if `execvp()` fails (surfacing a useful `PtySpawnError`).

### `bridge`
**`tunnelterm.bridge`** — `bridge_session()` is the byte pump between a live `WebSocket` and a `PtySession`:

1. Replays scrollback buffer to reconnecting client.
2. Re-applies last known PTY dimensions.
3. Runs two async tasks: `pty_to_ws` (PTY → WS) and `ws_to_pty` (WS → PTY).
4. On disconnect: `session.detach()`, keeps PTY alive if still alive.
5. If PTY died during bridge: notifies client with `{__tt: "process_exit"}`, then closes WS gracefully.

Text frames that contain `{__tt: ...}` JSON are treated as control frames (e.g. `resize`). Everything else is forwarded to the PTY. Frames over 1 MiB are rejected.

### `cookies`
**`tunnelterm.cookies`** — `tt_session` cookie (HttpOnly; Secure; SameSite=Strict). "Remember me" → persistent cookie (`Max-Age = token_ttl`); otherwise session-scoped. `read_ws_cookie()` also parses the raw `Cookie` header for WebSocket handshake compatibility.

### `middleware`
**`tunnelterm.middleware`** — `SecurityHeadersMiddleware` stamps CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and optionally HSTS onto every response.

### `routes`
**`routes/http_routes.py`** — `GET /` (serves SPA), `GET /healthz`.

**`routes/auth_routes.py`** — `POST /api/auth`, `POST /api/verify`, `POST /api/logout`. All check Origin allow-list and read `trusted_proxies.client_ip()`.

**`routes/terminal_ws.py`** — `WS /ws`. Origin check → cookie token → `try_acquire_session` (single-session lock) → `get_or_create` session → `bridge_session`.

## Security invariants

| Property | Mechanism |
|---|---|
| Token never in JS | HttpOnly cookie |
| CSRF blocked | SameSite=Strict cookie |
| Cross-site WS hijacking blocked | Origin allow-list (fail-closed on non-loopback binds) |
| Brute-force blocked | 5 failures/IP/15min → 5min lockout |
| Token unguessable | `secrets.token_urlsafe(32)` (256-bit) |
| Log safety | SHA-256 fingerprint of token in logs, never raw token |
| Single active session | `in_use` flag on token record; second WS connect is rejected |
| Rate limit on /verify | 300 hits/IP/min sliding window |
| XFF spoofing blocked | Only trust XFF from configured CIDRs (default: loopback) |
| Optional 2FA | RFC 6238 TOTP via `pyotp`, with ±1 step clock-skew window |

## Data flows

### Auth → session lifecycle

```
browser POST /api/auth (password, totp?)
  → Authenticator.verify(password)
  → (if require_totp) Authenticator.verify_totp(totp)
  → generate_token() → store in _tokens dict
  → set_session_cookie(token, HttpOnly; Secure; SameSite=Strict)
```

```
browser WS /ws (cookie)
  → token = read_ws_cookie()
  → check_auth(token)
  → try_acquire_session(token)  ← single-session lock
  → registry.get_or_create(token, loop)
    → PtySession.start() → pty.fork() + event-loop reader
  → bridge_session()  ← pty <-> ws
  → (disconnect) session.detach(); PTY stays alive
  → (reconnect) same token → registry.get() finds existing session
  → bridge_session(replay=True) → sends replay buffer first
```

### Idle timeout / reaper

```
SessionRegistry._reaper_loop() every 30s:
  → for each detached session where (now - last_detached_at) >= idle_timeout
  → discard(token) → PtySession.close() → SIGKILL process group → waitpid
```

### Logout

```
POST /api/logout
  → Authenticator.revoke(token)  ← token removed from _tokens
  → registry.discard(token)       ← PTY killed, session removed
  → clear_session_cookie()       ← cookie expires on client
```