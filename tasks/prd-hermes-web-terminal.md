[PRD]
# PRD: hermes-web-terminal

## Overview

A Python-based web terminal that hosts the `hermes` CLI tool, providing a browser-based UI with password authentication. Each browser session gets its own isolated `hermes` instance via PTY. The project is designed for production use with proper code quality standards (ruff + pyright).

## Goals

- Production-ready web interface for hermes CLI
- Password-based authentication via environment variable
- Config file support with environment variable override
- Proper PTY handling for TUI applications with debug logging
- Installable as a CLI tool via `uvx` or `uv run`
- Graceful shutdown with proper signal handling

## Quality Gates

These commands must pass for every user story:
- `uv run ruff check` - Linting with ruff
- `uv run ruff format` - Code formatting with ruff
- `uv run pyright` - Static type checking (no errors)

## User Stories

### US-001: Project Setup and Tooling Configuration
**Description:** Set up the uv project structure under `src/hermeswebterminal/` with proper ruff and pyright configuration.

**Acceptance Criteria:**
- [ ] Project initialized with `uv init`
- [ ] Package structure: `src/hermeswebterminal/__init__.py`, `src/hermeswebterminal/py.typed`
- [ ] `pyproject.toml` configured with ruff and pyright as dependencies
- [ ] `ruff.toml` or `[tool.ruff]` in pyproject.toml with strict settings
- [ ] `pyrightconfig.json` or `[tool.pyright]` configured
- [ ] Entry point defined as console script `hermes-web-terminal`
- [ ] All quality gates pass on initial setup

### US-002: PTY Manager Module
**Description:** Implement PTY spawning and I/O bridging to properly run hermes in a pseudo-terminal.

**Acceptance Criteria:**
- [ ] `src/hermeswebterminal/pty_manager.py` module created
- [ ] `spawn_hermes()` function using `pty.fork()` to create PTY
- [ ] Environment variables set: `TERM=xterm-256color`, proper `TERM_PROGRAM`
- [ ] Async I/O bridge using `asyncio` to connect PTY ↔ caller
- [ ] `read_from_pty()` generator yielding output bytes
- [ ] `write_to_pty()` async function for input
- [ ] Debug logging at each I/O step (configurable log level)
- [ ] Proper PTY cleanup on close

### US-003: Authentication Module
**Description:** Implement simple password-based authentication.

**Acceptance Criteria:**
- [ ] `src/hermeswebterminal/auth.py` module created
- [ ] Password loaded from `HERMES_WEB_TERMINAL_PASSWORD` env var
- [ ] `Authenticator` class with `verify(password) -> bool` method
- [ ] Session token generation using `secrets.token_urlsafe()`
- [ ] `check_auth(token) -> bool` for session validation
- [ ] Config file support: `~/.config/hermes-web-terminal/config.toml`
- [ ] Env vars override config file values
- [ ] Graceful error if password not set

### US-004: WebSocket Server
**Description:** Implement the WebSocket server to handle browser connections and bridge to PTY.

**Acceptance Criteria:**
- [ ] `src/hermeswebterminal/server.py` module created
- [ ] WebSocket endpoint at `/ws` using `websockets` library
- [ ] Auth gate: reject connection if not authenticated
- [ ] Spawn PTY on authenticated connection
- [ ] Bridge: PTY output → WebSocket → client
- [ ] Bridge: client input → WebSocket → PTY stdin
- [ ] Handle WebSocket disconnects gracefully
- [ ] Debug logging for all WebSocket messages (toggleable)
- [ ] Run on configurable host/port (default 127.0.0.1:4200)

### US-005: HTTP Server and Static Files
**Description:** Serve the xterm.js frontend via HTTP.

**Acceptance Criteria:**
- [ ] HTTP server on same port as WebSocket
- [ ] Serve `src/hermeswebterminal/static/index.html` at `/`
- [ ] Serve static assets if needed
- [ ] Index.html includes xterm.js from CDN
- [ ] xterm.js terminal connects to `/ws` on load

### US-006: Frontend Implementation
**Description:** Create the browser-based terminal UI with password prompt.

**Acceptance Criteria:**
- [ ] `src/hermeswebterminal/static/index.html` created
- [ ] Password input form displayed before terminal (if not authenticated)
- [ ] xterm.js terminal embedded after auth
- [ ] Keyboard input sent to WebSocket
- [ ] Terminal output rendered from WebSocket messages
- [ ] Simple, clean styling
- [ ] Connection status indicator

### US-007: CLI Entrypoint and Configuration
**Description:** Create the main CLI entrypoint with argument parsing and config loading.

**Acceptance Criteria:**
- [ ] `src/hermeswebterminal/__main__.py` as entry point
- [ ] CLI using `tyro` or `argparse` for argument parsing
- [ ] Arguments: `--port`, `--host`, `--password-env`, `--config`
- [ ] Load config from `~/.config/hermes-web-terminal/config.toml`
- [ ] Env vars override config file
- [ ] `--help` and `--version` support
- [ ] Start both HTTP and WebSocket servers

### US-008: Graceful Shutdown
**Description:** Implement proper signal handling for clean shutdown.

**Acceptance Criteria:**
- [ ] Handle `SIGINT` (Ctrl+C) gracefully
- [ ] Handle `SIGTERM` gracefully
- [ ] Kill spawned PTY processes on shutdown
- [ ] Close WebSocket connections cleanly
- [ ] Log shutdown sequence
- [ ] Exit with code 0 on clean shutdown

### US-009: Systemd Service File
**Description:** Provide optional systemd service for auto-start on boot.

**Acceptance Criteria:**
- [ ] `systemd/hermes-web-terminal.service` file created
- [ ] Correct paths for uvx execution
- [ ] Environment file support
- [ ] Restart policy configured
- [ ] Documentation in README

### US-010: README Documentation
**Description:** Document installation and usage.

**Acceptance Criteria:**
- [ ] Installation instructions
- [ ] Usage examples: `uvx hermes-web-terminal`, `uv run hermes-web-terminal`
- [ ] Configuration file example
- [ ] Environment variable reference
- [ ] Systemd setup instructions

## Functional Requirements

- **FR-1:** Each WebSocket connection spawns a new PTY with a fresh `hermes` instance
- **FR-2:** Password authentication is required before PTY is spawned
- **FR-3:** All PTY I/O must be logged at DEBUG level for troubleshooting
- **FR-4:** Graceful shutdown must terminate all child processes
- **FR-5:** Config file location: `~/.config/hermes-web-terminal/config.toml`
- **FR-6:** Default listen address: `127.0.0.1:4200` (not exposed publicly - nginx handles that)
- **FR-7:** WebSocket messages are raw bytes to preserve terminal semantics
- **FR-8:** PTY must have `TERM=xterm-256color` set

## Non-Goals

- Multiple users sharing the same `hermes` instance
- User management or per-user authentication
- SSH access or SSH proxying
- Terminal sharing or collaborative features
- Built-in SSL/TLS (handled by nginx reverse proxy)
- Horizontal scaling or multiple instances

## Technical Considerations

### Dependencies
- `websockets` - async WebSocket server (latest)
- `tyro` - CLI argument parsing (optional, `argparse` is alternative)
- Standard library: `pty`, `os`, `asyncio`, `signal`, `secrets`, `tomllib`

### Project Structure
```
src/hermeswebterminal/
├── __init__.py
├── __main__.py          # CLI entry point
├── py.typed             # Marker for type hints
├── auth.py              # Password authentication
├── pty_manager.py       # PTY spawning and I/O
├── server.py            # WebSocket + HTTP server
├── config.py            # Config loading
└── static/
    └── index.html       # xterm.js frontend
systemd/
└── hermes-web-terminal.service
pyproject.toml
ruff.toml
pyrightconfig.json
```

### Why Debug Logging Matters
With this custom implementation, we can add debug logging at each I/O step:
- Bytes received from WebSocket
- Bytes written to PTY stdin
- Bytes read from PTY stdout
- This will reveal exactly where input fails for hermes

## Open Questions

- Should there be a max session timeout?
- Should there be max number of concurrent sessions?
- Any specific xterm.js theme preferences?

[/PRD]
