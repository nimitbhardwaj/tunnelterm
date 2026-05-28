"""FastAPI server: HTTP/WebSocket bridge between browser and PTY."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from tunnelterm.auth import (
    RateLimitedError,
    get_authenticator,
    origin_allowed,
)
from tunnelterm.pty_manager import PtyManager, PtySpawnError

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
WS_SUBPROTOCOL = "tunnelterm.v1.token"

# Server -> Client control frames use this discriminator key. Any text frame
# missing this key is delivered verbatim to xterm. This avoids the "JSON output
# from the shell is silently swallowed" bug (e.g. echoing `{"foo":1}`).
CONTROL_KEY = "__tt"


_INDEX_HTML: bytes | None = None


def _read_index_html() -> bytes:
    """Return cached ``index.html`` bytes."""
    global _INDEX_HTML
    if _INDEX_HTML is None:
        _INDEX_HTML = (STATIC_DIR / "index.html").read_bytes()
    return _INDEX_HTML


# ---------- security headers ----------


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add a conservative CSP and friends to every HTTP response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Inject headers around the downstream response."""
        response = await call_next(request)
        # All scripts/styles served from same origin; no inline scripts allowed.
        # Inline styles are tolerated for now (xterm computes some styles inline).
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self' data:; "
                "img-src 'self' data:; "
                "connect-src 'self' ws: wss:; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        return response


# ---------- app factory ----------


def create_app(command: str, allowed_origins: list[str] | None = None) -> FastAPI:
    """Build the FastAPI app with the given runtime config."""
    app = FastAPI(title="tunnelterm")
    app.state.command = command
    app.state.allowed_origins = list(allowed_origins or [])

    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(content=_read_index_html())

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.websocket("/auth")
    async def auth_ws(ws: WebSocket) -> None:
        await _handle_auth(ws)

    @app.websocket("/logout")
    async def logout_ws(ws: WebSocket) -> None:
        await _handle_logout(ws)

    @app.websocket("/ws")
    async def ws_handler(ws: WebSocket) -> None:
        await _handle_terminal(ws)

    return app


# ---------- handlers ----------


def _check_origin(ws: WebSocket) -> bool:
    """Reject the handshake if Origin is not on the allow-list."""
    allowed = getattr(ws.app.state, "allowed_origins", []) or []
    origin = ws.headers.get("origin")
    return origin_allowed(origin, allowed)


async def _handle_auth(ws: WebSocket) -> None:
    """Password -> session token. Rate-limited per source IP."""
    if not _check_origin(ws):
        logger.warning("Rejecting /auth from disallowed origin %r", ws.headers.get("origin"))
        await ws.close(code=1008)
        return

    await ws.accept()
    client_ip = ws.client.host if ws.client else "unknown"
    auth = get_authenticator()

    try:
        auth.check_rate_limit(client_ip)
    except RateLimitedError as e:
        await ws.send_json({"error": "rate_limited", "retry_after": int(e.retry_after)})
        await ws.close(code=1008)
        return

    try:
        data = await ws.receive_json()
    except (json.JSONDecodeError, ValueError):
        await ws.send_json({"error": "invalid_json"})
        await ws.close()
        return
    except Exception:
        # WebSocketDisconnect or anything else.
        return

    password = data.get("password", "") if isinstance(data, dict) else ""
    if not isinstance(password, str):
        password = ""

    if auth.verify(password):
        auth.record_success(client_ip)
        token = auth.generate_token()
        logger.info("Auth success from %s", client_ip)
        await ws.send_json({"token": token})
    else:
        auth.record_failure(client_ip)
        logger.warning("Auth failure from %s", client_ip)
        await ws.send_json({"error": "invalid_password"})

    await ws.close()


async def _handle_logout(ws: WebSocket) -> None:
    """Revoke a token."""
    if not _check_origin(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        data = await ws.receive_json()
    except Exception:
        await ws.close()
        return
    token = data.get("token", "") if isinstance(data, dict) else ""
    if isinstance(token, str) and token:
        get_authenticator().revoke(token)
    await ws.send_json({"ok": True})
    await ws.close()


def _extract_token(ws: WebSocket) -> str | None:
    """Pull the token from the Sec-WebSocket-Protocol header.

    Clients send subprotocol == ``"tunnelterm.v1.token, <token>"``.
    """
    requested = ws.headers.get("sec-websocket-protocol", "")
    if not requested:
        return None
    parts = [p.strip() for p in requested.split(",")]
    if WS_SUBPROTOCOL not in parts:
        return None
    # The token is the *other* protocol element.
    for p in parts:
        if p and p != WS_SUBPROTOCOL:
            return p
    return None


async def _handle_terminal(ws: WebSocket) -> None:
    """Authenticate, spawn a PTY, and bridge to the WebSocket."""
    if not _check_origin(ws):
        logger.warning("Rejecting /ws from disallowed origin %r", ws.headers.get("origin"))
        await ws.close(code=1008)
        return

    token = _extract_token(ws)
    client_ip = ws.client.host if ws.client else "unknown"

    if not token:
        logger.warning("Unauthorized /ws from %s: no token", client_ip)
        await ws.close(code=1008)
        return

    auth = get_authenticator()
    if not auth.check_auth(token):
        logger.warning("Unauthorized /ws from %s: invalid token", client_ip)
        await ws.close(code=1008)
        return

    if not auth.try_acquire_session(token):
        logger.warning("Token already in use by another session (ip=%s)", client_ip)
        await ws.close(code=1008)
        return

    # Accept the handshake by echoing back our subprotocol identifier.
    await ws.accept(subprotocol=WS_SUBPROTOCOL)
    logger.info("Connection from %s", client_ip)

    command: str = getattr(ws.app.state, "command", "")
    pty_mgr = PtyManager(command=command)
    try:
        pty_mgr.spawn()
    except PtySpawnError as e:
        logger.error("PTY spawn failed: %s", e)
        try:
            await ws.send_json({CONTROL_KEY: "spawn_error", "message": str(e)})
        except Exception:
            pass
        await ws.close(code=1011)
        auth.release_session(token)
        return

    await _bridge(ws, pty_mgr, client_ip)
    auth.release_session(token)


async def _bridge(ws: WebSocket, pty_mgr: PtyManager, client_ip: str) -> None:
    """Bidirectional bridge between WebSocket and PTY master fd."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    # Bounded queue so a slow client back-pressures the reader instead of
    # silently dropping output.
    read_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=512)
    reader_paused = False
    pending_puts: set[asyncio.Task[None]] = set()

    master_fd = pty_mgr.master_fd
    reader_registered = False

    def _resume_reader() -> None:
        nonlocal reader_paused
        if reader_paused and pty_mgr.master_fd is not None:
            try:
                loop.add_reader(pty_mgr.master_fd, _on_pty_readable)
                reader_paused = False
            except (ValueError, OSError):
                pass

    def _pause_reader() -> None:
        nonlocal reader_paused
        if not reader_paused and pty_mgr.master_fd is not None:
            try:
                loop.remove_reader(pty_mgr.master_fd)
                reader_paused = True
            except (ValueError, OSError):
                pass

    def _on_pty_readable() -> None:
        """Handle readable PTY fd; pushes into read_queue. Runs on the loop."""
        fd = pty_mgr.master_fd
        if fd is None:
            try:
                read_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            data = os.read(fd, 4096)
        except (OSError, ValueError):
            try:
                read_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        if not data:
            try:
                read_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            read_queue.put_nowait(data)
        except asyncio.QueueFull:
            # Back-pressure: pause reading until consumer drains.
            _pause_reader()
            # Push the chunk back via a blocking put on the loop instead of dropping.
            # We can't await here (sync callback), so schedule a task to do it.
            task = loop.create_task(_blocking_put(data))
            pending_puts.add(task)
            task.add_done_callback(pending_puts.discard)

    async def _blocking_put(data: bytes) -> None:
        try:
            await read_queue.put(data)
        except asyncio.CancelledError:
            return
        # Now drained enough; resume.
        _resume_reader()

    if master_fd is not None:
        try:
            loop.add_reader(master_fd, _on_pty_readable)
            reader_registered = True
        except (ValueError, OSError) as e:
            logger.debug("add_reader failed: %s", e)

    async def pty_to_ws() -> None:
        while True:
            try:
                data = await read_queue.get()
            except asyncio.CancelledError:
                break
            if data is None:
                break
            try:
                await ws.send_bytes(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("ws.send_bytes failed: %s", e)
                break
            # If we were back-pressured, see if the queue has space again.
            if reader_paused and read_queue.qsize() < read_queue.maxsize // 2:
                _resume_reader()
        stop_event.set()

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                # Only treat as control frame if it parses as a dict AND has our
                # private discriminator. Anything else goes straight to the PTY.
                if msg.startswith("{") and CONTROL_KEY in msg:
                    try:
                        data = json.loads(msg)
                    except (json.JSONDecodeError, ValueError):
                        data = None
                    if isinstance(data, dict) and CONTROL_KEY in data:
                        await _handle_control(data, pty_mgr)
                        continue
                await pty_mgr.write_to_pty(msg.encode())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("WS->PTY ended: %s", e)
        finally:
            stop_event.set()

    pty_to_ws_task = asyncio.create_task(pty_to_ws())
    ws_to_pty_task = asyncio.create_task(ws_to_pty())

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.debug("ws_handler cancelled")
    finally:
        if reader_registered and pty_mgr.master_fd is not None:
            try:
                loop.remove_reader(pty_mgr.master_fd)
            except (ValueError, OSError):
                pass

        try:
            pty_mgr.close()
        except Exception as e:
            logger.debug("pty.close() error: %s", e)

        for task in (pty_to_ws_task, ws_to_pty_task):
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(pty_to_ws_task, ws_to_pty_task, return_exceptions=True),
                timeout=1.0,
            )
        except TimeoutError:
            logger.debug("Bridge tasks did not exit within 1s")
        except asyncio.CancelledError:
            pass

        try:
            await ws.send_json({CONTROL_KEY: "process_exit"})
        except Exception:
            pass
        logger.info("Connection closed for %s", client_ip)


async def _handle_control(msg: dict, pty_mgr: PtyManager) -> None:
    """Process a client -> server control frame."""
    kind = msg.get(CONTROL_KEY)
    if kind == "resize":
        try:
            cols = int(msg.get("cols", 0))
            rows = int(msg.get("rows", 0))
        except (TypeError, ValueError):
            return
        pty_mgr.resize(cols=cols, rows=rows)
    elif kind == "ping":
        # No-op; client uses RTT measurement via separate echo path.
        pass


# ---------- module-level app for uvicorn import-string mode ----------


# Default app exists so `uvicorn tunnelterm.main:app` works after configuration
# is set externally; normal flow uses create_app via run().
app = FastAPI(title="tunnelterm (uninitialized)")


def run(
    command: str,
    host: str = "127.0.0.1",
    port: int = 4200,
    log_level: str = "info",
    allowed_origins: list[str] | None = None,
) -> None:
    """Build the app and run uvicorn (blocking)."""
    import uvicorn

    application = create_app(command=command, allowed_origins=allowed_origins or [])
    uvicorn.run(application, host=host, port=port, log_level=log_level, access_log=False)
