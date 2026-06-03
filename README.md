# tunnelterm

A web-based terminal: spawn any shell command in a real PTY and interact with
it from the browser over WebSocket.

- Password-protected (per-IP rate limited)
- **Optional TOTP (RFC 6238) second factor** — Google Authenticator / 1Password / etc.
- **HttpOnly + SameSite=Strict session cookie** — JS never sees the token
- Single-active-session per token
- Fail-closed **Origin allow-list** required on non-loopback binds
- Token TTL + LRU eviction
- Sticky PTY sessions: page refresh keeps your shell alive
- Themes, fonts, search, web-links, WebGL renderer
- Persistent UI preferences in the browser

## Install

```bash
uv tool install tunnelterm
# or
uv pip install tunnelterm
```

## Run

`--command` is required. Examples:

```bash
TUNNELTERM_PASSWORD=hunter2 tunnelterm --command bash
TUNNELTERM_PASSWORD=hunter2 tunnelterm --command "zsh -l"
TUNNELTERM_PASSWORD=hunter2 tunnelterm --command htop --port 4242
```

Open `http://127.0.0.1:4200` and log in with the password.

### CLI options

| Option                    | Description                                                              | Default                              |
| ------------------------- | ------------------------------------------------------------------------ | ------------------------------------ |
| `--command`               | Command to run in the PTY. **Required.**                                 | —                                    |
| `--host`                  | Host to bind to                                                          | `127.0.0.1`                          |
| `--port`                  | Port to bind to                                                          | `4200`                               |
| `--password-env`          | Env var name to read the password from                                   | `TUNNELTERM_PASSWORD`                |
| `--config`                | Path to TOML config file                                                 | `~/.config/tunnelterm/config.toml`   |
| `--allowed-origin`        | Allowed `Origin` value (repeat). **Required on non-loopback binds.**     | —                                    |
| `--allow-any-origin`      | Disable Origin allow-list (unsafe; explicit escape hatch)                | off                                  |
| `--cookie-insecure`       | Omit `Secure` flag on cookies (only needed for plain-HTTP non-loopback)  | auto (Secure on, off on loopback)    |
| `--enable-hsts`           | Emit `Strict-Transport-Security` (HTTPS-only deployments)                | off                                  |
| `--session-idle-timeout`  | Seconds to keep an unattached PTY alive before reaping                   | `18000` (5h)                         |
| `--require-totp`          | Require a valid TOTP code in addition to the password on `/api/auth`     | off                                  |
| `--log-level`             | Log level: DEBUG, INFO, WARNING, ERROR                                   | INFO                                 |
| `--version`               | Print version and exit                                                   | —                                    |

### Environment variables

| Variable                          | Description                            |
| --------------------------------- | -------------------------------------- |
| `TUNNELTERM_PASSWORD`             | Password for authentication (REQUIRED) |
| `TUNNELTERM_HOST`                 | Bind host                              |
| `TUNNELTERM_PORT`                 | Bind port                              |
| `TUNNELTERM_COMMAND`              | PTY command (if `--command` not given) |
| `TUNNELTERM_ALLOWED_ORIGINS`      | Comma-separated `Origin` allow-list    |
| `TUNNELTERM_SESSION_IDLE_TIMEOUT` | Idle timeout in seconds                |
| `TUNNELTERM_TOTP_SECRET`          | Base32 TOTP shared secret (e.g. `JBSWY3DPEHPK3PXP`) |
| `TUNNELTERM_REQUIRE_TOTP`         | Set to `1` / `true` / `yes` to require TOTP on `/api/auth` |
| `LOG_LEVEL`                       | Log level fallback                     |

### Config file

`~/.config/tunnelterm/config.toml`:

```toml
password = "hunter2"
command = "bash"
host = "127.0.0.1"
port = 4200
allowed_origins = ["https://terminal.example.com"]
session_idle_timeout = 18000
enable_hsts = true

# Optional: TOTP second factor (Google Authenticator / 1Password / Authy / etc.)
totp_secret  = "JBSWY3DPEHPK3PXP"   # Base32 shared secret
require_totp = true                  # demand the code on /api/auth
```

`chmod 600` is recommended — tunnelterm logs a warning if the file is group-
or world-readable.

## Behind nginx with HTTPS (recommended for any non-loopback deployment)

Plaintext `ws://` is fine for `127.0.0.1` only. For any other deployment,
terminate TLS at a reverse proxy. Minimal nginx example:

```nginx
server {
    listen 443 ssl http2;
    server_name terminal.example.com;

    ssl_certificate     /etc/letsencrypt/live/terminal.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/terminal.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:4200;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        proxy_read_timeout 1h;
    }
}
```

Then run tunnelterm with:

```bash
tunnelterm \
  --command bash \
  --allowed-origin https://terminal.example.com \
  --enable-hsts
```

Cookies will automatically get `Secure` set because the bind is loopback while
the public scheme is HTTPS.

## Systemd

```bash
sudo cp systemd/tunnelterm.service /etc/systemd/system/
# Edit User=, WorkingDirectory=, and ExecStart=.
sudo mkdir -p /etc/tunnelterm
sudo cp systemd/env.example /etc/tunnelterm/env
sudo chmod 600 /etc/tunnelterm/env
sudo $EDITOR /etc/tunnelterm/env  # set TUNNELTERM_PASSWORD

sudo systemctl daemon-reload
sudo systemctl enable --now tunnelterm
sudo journalctl -u tunnelterm -f
```

## Session persistence

tunnelterm keeps your shell alive between connections so a page refresh, a
brief network drop, or closing and reopening a tab does *not* throw you back
into a fresh shell.

- Each session token has one PTY bound to it on the server. Reconnecting with
  the same token attaches you to the existing shell.
- On reattach the server replays the last 1 MB of PTY output, so the screen
  redraws to match what you were looking at.
- **"Keep me signed in"** on the login page makes the cookie persistent
  (`Max-Age = token TTL`, default 24h). Unchecked, it's a session cookie that
  dies when the browser closes.
- Sessions are reaped after `--session-idle-timeout` seconds of having no
  client attached (default 5h). The PTY is SIGKILLed.
- Explicit logout (settings drawer → Logout) revokes the token, kills the
  PTY, and clears the cookie immediately.

## Security model

### Two-factor authentication (TOTP)

When `totp_secret` is set **and** `require_totp = true` (or `--require-totp`
is passed), `/api/auth` demands a valid 6-digit TOTP code in addition to the
password. The code is verified against the shared Base32 secret using
[RFC 6238](https://www.rfc-editor.org/rfc/rfc6238) with a ±1 step (±30 s)
clock-skew window so phones with slightly off clocks still work.

To enable:

1. Generate a Base32 secret. The standard "Hello!" example is
   `JBSWY3DPEHPK3PXP`; in production, mint your own with e.g.
   `python -c 'import pyotp; print(pyotp.random_base32())'`.
2. Add the user to your authenticator app. The provisioning URI is
   `otpauth://totp/tunnelterm:<host>?secret=<BASE32>&issuer=tunnelterm` —
   most apps accept a manual entry of the secret.
3. Set `totp_secret` and `require_totp = true` in `config.toml` (or pass
   `TUNNELTERM_TOTP_SECRET` and `--require-totp`).

When TOTP is required the auth flow is:

```text
POST /api/auth  body: {"password": "...", "totp": "123456"}
```

A wrong TOTP returns `401 {"error": "invalid_credentials"}` — the same
response shape as a wrong password — so an attacker cannot tell which factor
was wrong. The TOTP failure counts toward the per-IP lockout (5 / 15 min).

If `totp_secret` is set but `require_totp` is off (the default), tunnelterm
will log a warning at startup: TOTP is configured but not enforced.

#### Login form behavior

The login form fetches `GET /api/auth/mode` at boot to learn whether TOTP
is required on this server. The form then renders accordingly:

| Server state | Form shows |
|---|---|
| `require_totp = true` | Password + TOTP field (always visible) |
| `require_totp = false`, no secret | Password only |
| `require_totp = false`, secret present | Password only (TOTP row stays hidden) |
| Probe failed (network) | Password only (degrades gracefully) |

The TOTP field is `inputmode="numeric"` with `autocomplete="one-time-code"`
so iOS / Android password managers and authenticator apps offer to fill it
automatically. The field accepts spaces in case the app shows the code as
"123 456"; whitespace is stripped before submission.

If the probe fails and the server actually does require TOTP, the user will
see a generic "Incorrect password or code" error after their first submit;
the form then reveals the TOTP field with focus moved to it, so they can
add the code on the next attempt. This degrades safely without revealing
whether the server requires TOTP to a network observer.

### Token storage
- The session token is stored in an `HttpOnly; Secure; SameSite=Strict`
  cookie. JavaScript cannot read or exfiltrate it, even via XSS.
- The browser carries the cookie automatically on `/api/*` POSTs and the
  `/ws` WebSocket upgrade.

### CSRF / CSWSH
- `SameSite=Strict` blocks the cookie on cross-site requests.
- An additional Origin allow-list is enforced on every POST and WebSocket
  handshake. The server **refuses to start** on a non-loopback bind unless
  `--allowed-origin` is given (or `--allow-any-origin` explicitly opted into).

### Brute force / abuse
- 5 failed `/api/auth` attempts from one IP within 15 min → 5 min lockout.
- `/api/verify` is rate-limited at 60 hits/IP/min.
- WebSocket text frames are capped at 1 MiB to prevent memory exhaustion.

### Lifecycle
- Tokens expire after 24 h by default; LRU-evicted at 64 outstanding.
- Each token may only be active in one WebSocket connection at a time.
- Log lines include a SHA-256 fingerprint of the token, never the token itself.

### HTTP hardening
- CSP: `default-src 'self'; script-src 'self'; connect-src 'self'` etc.
  No wildcard ws/wss, no inline scripts.
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy` locking down hardware APIs.
- Optional `Strict-Transport-Security` via `--enable-hsts`.
- xterm assets are vendored locally with SRI hashes; no third-party CDN trust.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run pyright
```
