# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

*Add reusable patterns discovered during development here.*

**websocket.request.path vs websocket.request.uri**: In websockets library, ServerConnection.request has `.path` (the path string like "/ws") not `.uri`. The `.request` attribute is `None` until handshake completes, so always guard with `if websocket.request`.

**PTY async I/O pattern**: When bridging PTY file descriptors to asyncio, use `socket.fromfd()` to convert the raw fd into a socket object that works with `loop.sock_sendall()` and `loop.sock_recv()`. Direct fd operations in async context cause type errors.

**secrets.compare_digest for timing-safe comparison**: Always use `secrets.compare_digest(a, b)` instead of `a == b` for comparing passwords/tokens to prevent timing attacks.

**Type narrowing after guards**: When checking `if x is None: raise; self._x = x`, pyright may still see `x` as `None` in the assignment. Use explicit type annotation like `self._password: str` before the guard, or assign to a local variable first.

**asyncio StreamReader/Writer HTTP serving**: Use `asyncio.StreamReader` and `asyncio.StreamWriter` for low-level HTTP serving. Read request line with `await reader.readline()`, decode bytes with `.decode("utf-8")`, write raw bytes directly with `writer.write()` and drain with `await writer.drain()`. Always close writer in finally block.

**websockets ServerConnection type**: Use `websockets.asyncio.server.ServerConnection` for type hints. The handler receives this type. Access request headers via `websocket.request.headers` (not `request_headers`). The `request` attribute is `None` until the handshake completes, so guard with `if websocket.request`.

**websockets async iteration**: When iterating `async for message in websocket:`, the message type is `Data` which can be `bytes | str`. Must check `isinstance(message, bytes)` and convert to bytes before passing to PTY writer that expects `bytes`.

---

## 2026-05-28 - US-010
- What was implemented
  - Expanded README.md with comprehensive documentation
  - Added usage examples: uvx, uv run, and installed package usage
  - Added command line options table
  - Added configuration file example with all supported options
  - Added environment variable reference table
  - Systemd setup instructions already existed, kept in place

- Files changed
  - Modified: README.md (added installation, usage, configuration, env var reference sections)

- **Learnings:**
  - README documentation helps users understand how to use the tool properly

---

## 2026-05-28 - US-004
- What was implemented
  - Updated src/hermeswebterminal/server.py with WebSocket endpoint at /ws
  - Auth gate: rejects connection if Authorization Bearer header missing or invalid
  - Spawns PtyManager on authenticated connection
  - Bridge: PTY output → WebSocket → client (read_from_pty_async → websocket.send)
  - Bridge: client input → WebSocket → PTY stdin (websocket iteration → write_to_pty)
  - Handles WebSocket disconnects gracefully with try/finally cleanup
  - Debug logging for all WebSocket messages (toggleable via DEBUG_MESSAGES flag)
  - Configurable host/port via run_server() (default 127.0.0.1:4200)
  - websockets library added to dependencies

- Files changed
  - Modified: src/hermeswebterminal/server.py (WebSocket handler + bridging logic)
  - Modified: pyproject.toml (added websockets dependency)

- **Learnings:**
  - websockets.asyncio.server.ServerConnection has `.request` attribute that is `None` until handshake completes; guard with `if websocket.request` before accessing
  - When iterating WebSocket messages with `async for message in websocket:`, message type is `Data` (bytes | str), must convert to bytes before passing to PTY
  - Use `websockets.serve()` to create WebSocket server (returns WebSocketServer object, not a coroutine directly)
  - ws_server is an async context manager that you `async with` alongside the HTTP server

---

## 2026-05-28 - US-006
- What was implemented
  - Enhanced index.html with password login form before terminal
  - Added /auth/login HTTP endpoint for authentication
  - WebSocket now receives auth_token as query parameter instead of Authorization header
  - Login form styled with clean dark theme, connection status indicator
  - Terminal only shown after successful authentication
  - xterm.js connects to WebSocket with auth_token query param

- Files changed
  - Modified: src/hermeswebterminal/static/index.html (added login form, status indicator, auth flow)
  - Modified: src/hermeswebterminal/server.py (added /auth/login endpoint, updated WebSocket auth to use query param)

- **Learnings:**
  - WebSocket auth via query param `?auth_token=` is simpler for browser clients than header-based auth
  - ServerConnection.request.path not .uri (path is just the path portion, not full URI)
  - Use urllib.parse.parse_qs to extract query params from path

---

## 2026-05-28 - US-005
- What was implemented
  - Created src/hermeswebterminal/static/ directory with index.html
  - index.html includes xterm.js v5.5.0 from CDN (jsdelivr)
  - xterm.js terminal connects to /ws on load using WebSocket
  - Created src/hermeswebterminal/server.py with HTTP request handler
  - handle_http_request() serves index.html at / using asyncio StreamReader/Writer
  - Static files served from package directory using Path(__file__).parent

- Files changed
  - Created: src/hermeswebterminal/static/index.html
  - Created: src/hermeswebterminal/server.py

- **Learnings:**
  - xterm.js event is 'onmessage', not 'ondata' (that was a mistake to fix)
  - W292 no newline at end of file requires explicit echo "" >> file or rewriting
  - WebSocket API uses .onmessage for receiving, .send() for sending, .onopen for connection opened

---

## 2026-05-28 - US-001
- What was implemented
  - Created src/hermeswebterminal/ package structure with __init__.py and py.typed
  - Created src/hermeswebterminal/main.py with run() function for entry point
  - Updated existing main.py to add docstring for lint compliance
  - Verified all quality checks pass: ruff check, ruff format, pyright

- Files changed
  - Created: src/hermeswebterminal/__init__.py (empty, marks package)
  - Created: src/hermeswebterminal/py.typed (marks package as typed)
  - Created: src/hermeswebterminal/main.py (run() function)
  - Modified: main.py (added docstring, return type annotation)

- **Learnings:**
  - pyproject.toml [tool.ruff.lint] rules like D100/D104 (missing docstrings) apply even to empty __init__.py - these are already ignored in config
  - Empty files need newline at end to pass W292
  - uv run creates .venv on first use which takes time
  - pyrightconfig.json is optional - [tool.pyright] in pyproject.toml works fine
  - ruff format --check shows "Would reformat" but doesn't fail; ruff format --diff shows actual diff
  - Entry point hermes-web-terminal = "hermeswebterminal.main:run" expects module hermeswebterminal.main with run() function

---

## 2026-05-28 - US-003
- What was implemented
  - Created src/hermeswebterminal/auth.py with Authenticator class
  - Password loaded from HERMES_WEB_TERMINAL_PASSWORD env var (takes precedence)
  - Falls back to ~/.config/hermes-web-terminal/config.toml password field
  - Authenticator.verify(password) -> bool using secrets.compare_digest
  - Session tokens via secrets.token_urlsafe(), stored in instance set
  - check_auth(token) -> bool validates against stored tokens
  - AuthenticationError raised if no password configured

- Files changed
  - Created: src/hermeswebterminal/auth.py

- **Learnings:**
  - Session tokens must be stored in memory to validate later (using set)
  - tomllib.load() requires binary mode ("rb") file opening
  - isort auto-fixes import order when running `ruff check --fix`
  - Type narrowing: after `if x is None: raise`, pyright may still see x as None on next line; need explicit annotation or intermediate variable

---

## 2026-05-28 - US-007
- What was implemented
  - Created src/hermeswebterminal/__main__.py as CLI entrypoint using argparse
  - Arguments: --port, --host, --password-env, --config, --help, --version
  - Load config from ~/.config/hermes-web-terminal/config.toml
  - Env vars override config file values
  - Starts both HTTP and WebSocket servers via run_server()
  - Updated main.py to re-export main from __main__ for entry point compatibility

- Files changed
  - Created: src/hermeswebterminal/__main__.py (CLI entrypoint with argparse)
  - Modified: src/hermeswebterminal/main.py (re-exports main from __main__)

- **Learnings:**
  - `if __name__ == "__main__": main()` at end of __main__.py allows `python -m module` to work
  - argparse `action="version"` takes format string, not direct version string
  - W292 no newline at end of file - use `echo "" >> file` or the file write tool handles it
  - `uv pip install -e .` needed to test package before running python -m

---

## 2026-05-28 - US-002
- What was implemented
  - Created src/hermeswebterminal/pty_manager.py with PtyManager class
  - spawn_hermes() function using pty.fork() to create PTY
  - Environment variables set: TERM=xterm-256color, TERM_PROGRAM=hermes-web-terminal
  - Async I/O bridge using asyncio with read_from_pty_async() and write_to_pty()
  - Sync read_from_pty() generator using os.read
  - Debug logging at each I/O step with configurable log level
  - Proper PTY cleanup on close() with SIGTERM and fd/socket closing

- Files changed
  - Created: src/hermeswebterminal/pty_manager.py

- **Learnings:**
  - PTY file descriptors need socket.fromfd() conversion to work with asyncio loop.sock_sendall/recv
  - pty.fork() returns (pid, master_fd) tuple, not a single value
  - Docstring sections (Args, Returns, Raises, Yields) need blank line after them (D413)
  - Imports from typing module like AsyncGenerator/Generator should come from collections.abc (UP035)
  - W292 no newline at end of file requires explicit echo "" >> file to fix
---

## 2026-05-28 - US-009
- What was implemented
  - Created systemd/hermes-web-terminal.service with proper configuration
  - EnvironmentFile support via /etc/hermes-web-terminal/env
  - Restart policy: Restart=always, RestartSec=5
  - Created systemd/env.example template
  - Updated README.md with systemd installation instructions

- Files changed
  - Created: systemd/hermes-web-terminal.service
  - Created: systemd/env.example
  - Modified: README.md (added systemd service documentation)

- **Learnings:**
  - systemd service files use absolute paths for ExecStart commands
  - EnvironmentFile directive loads env vars from a file (not shell expansion)
  - uv run --package can run installed packages directly

---

## 2026-05-28 - US-008
- What was implemented
  - Graceful shutdown handling for SIGINT and SIGTERM signals
  - `asyncio.Event` as shutdown coordination mechanism between signal handler and server
  - Signal handlers registered in `main()` using `signal.signal()`
  - `run_server()` accepts optional `shutdown_event` parameter; waits on it if provided
  - PTY processes already killed via `pty_manager.close()` in websocket_handler's finally block
  - WebSocket connections closed cleanly via websockets library's async context manager
  - Shutdown sequence logged (signal received, closing connections, killing PTY, exiting)
  - Exit with code 0 on clean shutdown (no explicit `sys.exit(0)` needed - natural exit)

- Files changed
  - Modified: src/hermeswebterminal/server.py (added shutdown_event parameter to run_server)
  - Modified: src/hermeswebterminal/__main__.py (added signal handlers, shutdown event, removed sys.exit)

- **Learnings:**
  - Use `asyncio.Event` to coordinate shutdown between signal handlers (sync context) and async server
  - Signal handlers must be set before `asyncio.run()` - cannot be set inside async context
  - `signal.signal(sig, handler)` takes an integer for sig, not the signal module constant directly in the callback
  - Removing `sys.exit(0)` allows natural exit with code 0 - asyncio.run() completes and main() returns
  - websockets `async with server, ws_server:` handles clean WebSocket connection closure on exit

---
