"""FastAPI server: HTTP/WebSocket bridge between browser and PTY."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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
from tunnelterm.pty_manager import PtySpawnError
from tunnelterm.session import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    PtySession,
    SessionRegistry,
)

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


def create_app(
    command: str,
    allowed_origins: list[str] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> FastAPI:
    """Build the FastAPI app with the given runtime config."""
    registry = SessionRegistry(command=command, idle_timeout=idle_timeout)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        registry.start(loop)
        logger.info(
            "session registry online (idle_timeout=%.0fs, command=%r)",
            idle_timeout,
            command,
        )
        try:
            yield
        finally:
            await registry.shutdown()
            logger.info("session registry shut down")

    app = FastAPI(title="tunnelterm", lifespan=lifespan)
    app.state.command = command
    app.state.allowed_origins = list(allowed_origins or [])
    app.state.idle_timeout = idle_timeout
    app.state.registry = registry

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

    @app.websocket("/verify")
    async def verify_ws(ws: WebSocket) -> None:
        await _handle_verify(ws)

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


async def _handle_verify(ws: WebSocket) -> None:
    """Check whether a stored client token is still valid.

    Used by the "Remember me" flow on page load: client sends ``{"token": ...}``
    and gets back ``{"ok": true|false}``. Does not consume a rate-limit slot
    because the token is opaque and not a brute-forceable secret in the same
    way a password is.
    """
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
    ok = isinstance(token, str) and bool(token) and get_authenticator().check_auth(token)
    try:
        await ws.send_json({"ok": ok})
    finally:
        await ws.close()


async def _handle_logout(ws: WebSocket) -> None:
    """Revoke a token and discard its sticky PTY session."""
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
        registry: SessionRegistry | None = getattr(ws.app.state, "registry", None)
        if registry is not None:
            registry.discard(token)
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
    """Authenticate, attach to a (new or existing) sticky session, bridge IO."""
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
        logger.warning("Token already in use by another connection (ip=%s)", client_ip)
        await ws.close(code=1008)
        return

    # Accept the handshake by echoing back our subprotocol identifier.
    await ws.accept(subprotocol=WS_SUBPROTOCOL)

    registry: SessionRegistry = ws.app.state.registry
    loop = asyncio.get_running_loop()
    # Look up first so we know whether we're reattaching or creating.
    was_existing = registry.get(token) is not None
    try:
        session = registry.get_or_create(token, loop=loop)
    except PtySpawnError as e:
        logger.error("PTY spawn failed: %s", e)
        try:
            await ws.send_json({CONTROL_KEY: "spawn_error", "message": str(e)})
        except Exception:
            pass
        await ws.close(code=1011)
        auth.release_session(token)
        return

    logger.info(
        "Connection from %s (%s session for token=%s...)",
        client_ip,
        "reattaching to" if was_existing else "new",
        token[:8],
    )

    try:
        await _bridge(ws, session, client_ip, replay=was_existing)
    finally:
        auth.release_session(token)


# ---------- bridge ----------


async def _bridge(
    ws: WebSocket,
    session: PtySession,
    client_ip: str,
    replay: bool,
) -> None:
    """Pump bytes between ``ws`` and ``session`` until either side disconnects.

    The session keeps owning the PTY; this function attaches a queue-pushing
    callback so we receive PTY output, and forwards ``ws.receive_text`` into
    ``session.write``. When the WebSocket goes away, the session is *detached*
    (PTY keeps running) so a refresh can reattach.
    """
    stop_event = asyncio.Event()
    # Bounded queue so a slow client back-pressures (we drop oldest if it stays
    # full; a sticky-session client that vanished is reaped on idle, not here).
    out_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1024)

    def _on_data(chunk: bytes) -> None:
        """Session data callback (runs on the loop thread)."""
        if not chunk:
            # PTY EOF -> shell exited. Signal pty_to_ws to drain and stop.
            try:
                out_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            out_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # Slow consumer; drop the oldest queued item to maintain liveness.
            try:
                out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                out_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    # Replay scrollback to the new client *before* attaching the live listener,
    # so the replay is sent in order ahead of any new bytes.
    if replay:
        snapshot = session.replay_buffer()
        if snapshot:
            try:
                await ws.send_bytes(snapshot)
            except Exception as e:
                logger.debug("scrollback replay send failed: %s", e)

    session.attach(_on_data)

    # Re-apply last known terminal dimensions so the shell isn't confused after
    # reattach. The client will also send its own resize within a few ms.
    cols, rows = session.dimensions
    session.resize(cols=cols, rows=rows)

    async def pty_to_ws() -> None:
        while True:
            try:
                data = await out_queue.get()
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
        stop_event.set()

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                # Treat as a control frame only if it parses as a dict AND has
                # our discriminator key. Anything else is opaque shell input.
                if msg.startswith("{") and CONTROL_KEY in msg:
                    try:
                        data = json.loads(msg)
                    except (json.JSONDecodeError, ValueError):
                        data = None
                    if isinstance(data, dict) and CONTROL_KEY in data:
                        await _handle_control(data, session)
                        continue
                await session.write(msg.encode())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("WS->PTY ended: %s", e)
        finally:
            stop_event.set()

    pty_to_ws_task = asyncio.create_task(pty_to_ws())
    ws_to_pty_task = asyncio.create_task(ws_to_pty())

    pty_died = False
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.debug("ws_handler cancelled")
    finally:
        # Detach BEFORE checking PTY liveness; we don't want stray writes.
        session.detach(_on_data)

        # If the PTY itself died (shell exited / crashed), tear the session
        # down so the next attach with this token spawns a fresh shell.
        pty_died = not session.is_alive()
        if pty_died:
            registry: SessionRegistry | None = getattr(ws.app.state, "registry", None)
            if registry is not None:
                registry.discard(session.token)

        # Cancel any in-flight bridge tasks.
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

        if pty_died:
            # Notify client and close cleanly. Without an explicit ws.close()
            # FastAPI tears down the TCP socket without a WebSocket close
            # frame, which the browser sees as code 1006.
            try:
                await ws.send_json({CONTROL_KEY: "process_exit"})
            except Exception:
                pass
            try:
                if ws.client_state.name != "DISCONNECTED":
                    await ws.close(code=1000)
            except Exception as e:
                logger.debug("ws.close() error: %s", e)
            logger.info(
                "Session ended for %s (token=%s...; PTY exited)",
                client_ip,
                session.token[:8],
            )
        else:
            # Soft disconnect (refresh / network blip). PTY stays alive, the
            # idle reaper will collect it if no one reattaches in time.
            logger.info(
                "Connection detached for %s (token=%s...; session kept alive)",
                client_ip,
                session.token[:8],
            )
        # Use `pty_died` to silence ruff's "assigned but not used" warning.
        _ = pty_died


async def _handle_control(msg: dict, session: PtySession) -> None:
    """Process a client -> server control frame."""
    kind = msg.get(CONTROL_KEY)
    if kind == "resize":
        try:
            cols = int(msg.get("cols", 0))
            rows = int(msg.get("rows", 0))
        except (TypeError, ValueError):
            return
        session.resize(cols=cols, rows=rows)
    elif kind == "ping":
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
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> None:
    """Build the app and run uvicorn (blocking)."""
    import uvicorn

    application = create_app(
        command=command,
        allowed_origins=allowed_origins or [],
        idle_timeout=idle_timeout,
    )
    uvicorn.run(application, host=host, port=port, log_level=log_level, access_log=False)
