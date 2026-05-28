# tunnelterm

A web-based terminal: spawn any shell command in a real PTY and interact with
it from the browser over WebSocket.

- Password-protected (per-IP rate limited)
- Single-active-session per token
- Origin allow-list for the WebSocket handshake
- Token TTL + LRU eviction
- Themes, fonts, search, web-links, WebGL renderer
- Persistent preferences in the browser

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

| Option              | Description                                                    | Default                |
| ------------------- | -------------------------------------------------------------- | ---------------------- |
| `--command`         | Command to run in the PTY. **Required.**                       | —                      |
| `--host`            | Host to bind to                                                | `127.0.0.1`            |
| `--port`            | Port to bind to                                                | `4200`                 |
| `--password-env`    | Env var name to read the password from                         | `TUNNELTERM_PASSWORD`  |
| `--config`          | Path to TOML config file                                       | `~/.config/tunnelterm/config.toml` |
| `--allowed-origin`  | Allowed `Origin` header (repeat to allow multiple)             | accept all             |
| `--session-idle-timeout` | Seconds to keep a sticky PTY alive after the WebSocket disconnects | `18000` (5h)       |
| `--log-level`       | Log level: DEBUG, INFO, WARNING, ERROR                         | INFO                   |
| `--version`         | Print version and exit                                         | —                      |

### Environment variables

| Variable                       | Description                            |
| ------------------------------ | -------------------------------------- |
| `TUNNELTERM_PASSWORD`          | Password for authentication (REQUIRED) |
| `TUNNELTERM_HOST`              | Bind host                              |
| `TUNNELTERM_PORT`              | Bind port                              |
| `TUNNELTERM_COMMAND`           | PTY command (if `--command` not given) |
| `TUNNELTERM_ALLOWED_ORIGINS`   | Comma-separated `Origin` allow-list    |
| `TUNNELTERM_SESSION_IDLE_TIMEOUT` | Idle timeout in seconds (see `--session-idle-timeout`) |
| `LOG_LEVEL`                    | Log level fallback                     |

### Config file

`~/.config/tunnelterm/config.toml`:

```toml
password = "hunter2"
command = "bash"
host = "127.0.0.1"
port = 4200
allowed_origins = ["https://terminal.example.com"]
```

`chmod 600` is recommended — tunnelterm logs a warning if the file is group-
or world-readable.

## Behind nginx with HTTPS

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

Then run tunnelterm with `--allowed-origin https://terminal.example.com`.

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

- Each auth token has one PTY bound to it on the server. Reconnecting with the
  same token attaches you to the existing shell.
- On reattach the server replays the last 1 MB of PTY output, so the screen
  redraws to match what you were looking at.
- "Remember me" on the login page stores the token in `localStorage`
  (survives browser restart). Otherwise it goes to `sessionStorage` (this tab
  only).
- Sessions are reaped after `--session-idle-timeout` seconds of having no
  client attached (default 5h). The PTY is SIGKILLed.
- Explicit logout (settings drawer → Logout) revokes the token and kills the
  PTY immediately.

## Security notes

- Tokens are sent via `Sec-WebSocket-Protocol` (not the URL), so reverse-proxy
  access logs do not capture them.
- Each token may only be active in one WebSocket connection at a time.
- Tokens expire after 24h by default; LRU-evicted at 64 outstanding.
- Five failed auth attempts in 15min lock the source IP out for 5min.
- Origin allow-list prevents cross-site WebSocket hijacking.
- All HTTP responses ship CSP, X-Content-Type-Options, X-Frame-Options.
- xterm assets are vendored locally; no third-party CDN trust.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run pyright
```
