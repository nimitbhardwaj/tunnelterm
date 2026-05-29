"""FastAPI app factory and uvicorn entrypoint.

The actual request handlers live under :mod:`tunnelterm.routes`. The
WebSocket bridge is in :mod:`tunnelterm.bridge`. Security middleware is in
:mod:`tunnelterm.middleware`. Cookie helpers are in :mod:`tunnelterm.cookies`.

This file's job is to wire those parts together and to expose two callables:

* :func:`create_app` — for tests and ASGI servers
* :func:`run` — for the CLI entrypoint
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from tunnelterm.middleware import SecurityHeadersMiddleware
from tunnelterm.routes.auth_routes import router as auth_router
from tunnelterm.routes.http_routes import router as http_router
from tunnelterm.routes.terminal_ws import router as ws_router
from tunnelterm.session import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    SessionRegistry,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    command: str,
    allowed_origins: list[str] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    *,
    cookie_secure: bool = False,
    allow_any_origin: bool = False,
    enable_hsts: bool = False,
) -> FastAPI:
    """Build the FastAPI app with the given runtime config.

    Args:
        command: Shell command to spawn in the PTY.
        allowed_origins: Hostnames allowed in the ``Origin`` header.
        idle_timeout: Seconds to keep an unattached PTY alive before reaping.
        cookie_secure: Whether to set ``Secure`` on session cookies (HTTPS-only).
        allow_any_origin: Explicit escape hatch to disable the Origin allow-list.
        enable_hsts: Add ``Strict-Transport-Security`` (HTTPS deployments only).

    """
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
    app.state.cookie_secure = cookie_secure
    app.state.allow_any_origin = allow_any_origin

    app.add_middleware(SecurityHeadersMiddleware, enable_hsts=enable_hsts)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(http_router)
    app.include_router(auth_router)
    app.include_router(ws_router)

    return app


# A module-level placeholder so `uvicorn tunnelterm.main:app` still works for
# advanced users who would rather configure via environment than via the CLI.
# Production code paths go through :func:`run`.
app = FastAPI(title="tunnelterm (uninitialized)")


def run(
    command: str,
    host: str = "127.0.0.1",
    port: int = 4200,
    log_level: str = "info",
    allowed_origins: list[str] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    *,
    cookie_secure: bool = False,
    allow_any_origin: bool = False,
    enable_hsts: bool = False,
) -> None:
    """Build the app and run uvicorn (blocking)."""
    import uvicorn

    application = create_app(
        command=command,
        allowed_origins=allowed_origins or [],
        idle_timeout=idle_timeout,
        cookie_secure=cookie_secure,
        allow_any_origin=allow_any_origin,
        enable_hsts=enable_hsts,
    )
    uvicorn.run(application, host=host, port=port, log_level=log_level, access_log=False)
