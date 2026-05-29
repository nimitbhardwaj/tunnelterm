"""HTTP middleware shipped with tunnelterm.

Currently a single :class:`SecurityHeadersMiddleware` that stamps a
conservative CSP, anti-clickjacking, MIME-sniffing, and referrer hardening
headers onto every response.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add a conservative CSP and a few well-known security headers.

    Args:
        app: The wrapped ASGI app.
        enable_hsts: Whether to emit ``Strict-Transport-Security`` (only safe
            when the deployment is HTTPS-only).

    """

    def __init__(self, app, enable_hsts: bool = False) -> None:
        """Initialize."""
        super().__init__(app)
        self._enable_hsts = enable_hsts

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Inject headers around the downstream response."""
        response = await call_next(request)
        # All scripts/styles served from same origin; no inline scripts allowed.
        # `connect-src 'self'` covers same-origin ws:// and wss:// automatically;
        # we deliberately do NOT add a wildcard ws:/wss: source.
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self' data:; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
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
        if self._enable_hsts:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response
