"""WebSocket route for the live terminal connection.

Only ``/ws`` lives here. Auth (HTTP, cookie-based) is in :mod:`auth_routes`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket

from tunnelterm.auth import (
    get_authenticator,
    origin_allowed,
    token_fingerprint,
)
from tunnelterm.bridge import bridge_session
from tunnelterm.constants import CONTROL_KEY
from tunnelterm.cookies import read_ws_cookie
from tunnelterm.pty_manager import PtySpawnError
from tunnelterm.session import SessionRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_origin(ws: WebSocket) -> bool:
    """Allow the handshake only when the Origin matches the configured allow-list.

    Honors ``app.state.allow_any_origin`` as an explicit escape hatch.
    """
    if getattr(ws.app.state, "allow_any_origin", False):
        return True
    allowed = list(getattr(ws.app.state, "allowed_origins", []) or [])
    origin = ws.headers.get("origin")
    return origin_allowed(origin, allowed)


def _client_ip(ws: WebSocket) -> str:
    """Return the real client IP, honouring XFF only from a trusted proxy."""
    xff = ws.headers.get("X-Forwarded-For")
    return ws.app.state.trusted_proxies.client_ip(
        ws.client.host if ws.client else "",
        xff,
    )


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """Authenticate via cookie, attach to a sticky session, run the bridge."""
    if not _check_origin(ws):
        logger.warning("Rejecting /ws from disallowed origin %r", ws.headers.get("origin"))
        await ws.close(code=1008)
        return

    token = read_ws_cookie(ws)
    client_ip = _client_ip(ws)

    if not token:
        logger.warning("Unauthorized /ws from %s: no session cookie", client_ip)
        await ws.close(code=1008)
        return

    auth = get_authenticator()
    if not auth.check_auth(token):
        logger.warning("Unauthorized /ws from %s: invalid token", client_ip)
        await ws.close(code=1008)
        return

    if not auth.try_acquire_session(token):
        logger.warning(
            "Token already in use by another connection (ip=%s, token=%s)",
            client_ip,
            token_fingerprint(token),
        )
        await ws.close(code=1008)
        return

    await ws.accept()

    registry: SessionRegistry = ws.app.state.registry
    import asyncio

    loop = asyncio.get_running_loop()
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
        "Connection from %s (%s session, token=%s)",
        client_ip,
        "reattaching to" if was_existing else "new",
        token_fingerprint(token),
    )

    try:
        await bridge_session(
            ws=ws,
            session=session,
            registry=registry,
            client_ip=client_ip,
            replay=was_existing,
        )
    finally:
        auth.release_session(token)
