"""HTTP auth endpoints: /api/auth, /api/verify, /api/logout.

All three are plain ``POST``s that read/write the session cookie:

* ``/api/auth``   — verify password, mint a token, ``Set-Cookie`` it.
* ``/api/verify`` — check whether the cookie is still valid (no body needed).
* ``/api/logout`` — revoke the cookie's token and discard the sticky session.

The cookie is ``HttpOnly; Secure; SameSite=Strict`` so JavaScript cannot read
it (XSS-safe) and browsers won't send it on cross-site requests (CSRF-safe).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from tunnelterm.auth import (
    RateLimitedError,
    get_authenticator,
    origin_allowed,
    token_fingerprint,
)
from tunnelterm.cookies import (
    clear_session_cookie,
    read_session_cookie,
    set_session_cookie,
)

if TYPE_CHECKING:
    from tunnelterm.session import SessionRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _origin_check_ok(request: Request) -> bool:
    """Reject cross-site state-changing requests based on the configured allow-list.

    SameSite=Strict already blocks third-party form submits and XHRs from
    setting the cookie, but the request itself can still arrive (e.g.
    fetched without credentials). We additionally validate ``Origin`` /
    ``Referer`` to keep error responses from leaking.

    Safe methods (GET, HEAD, OPTIONS) are exempt: they have no side effects,
    and per the Fetch spec browsers do NOT send an ``Origin`` header on
    same-origin GETs -- only on cross-origin requests and on
    state-changing methods. Enforcing the allow-list on same-origin reads
    would 403 the login form's ``/api/auth/mode`` probe (which has no
    Origin header in the browser), leaving the TOTP field hidden until
    the user makes a first failed submit.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    allowed: list[str] = list(getattr(request.app.state, "allowed_origins", []) or [])
    allow_any: bool = bool(getattr(request.app.state, "allow_any_origin", False))
    if allow_any:
        return True
    origin = request.headers.get("origin") or request.headers.get("referer")
    return origin_allowed(origin, allowed)


def _client_ip(request: Request) -> str:
    """Return the real client IP, honouring XFF only from a trusted proxy."""
    xff = request.headers.get("X-Forwarded-For")
    return request.app.state.trusted_proxies.client_ip(
        request.client.host if request.client else "",
        xff,
    )


def _cookie_secure(request: Request) -> bool:
    """Return True if we should set ``Secure`` on outgoing cookies.

    When behind a trusted reverse proxy that sends ``X-Forwarded-Proto``,
    honour it so that ``Secure`` is set correctly even when the app listens
    on plain HTTP (e.g. loopback). If no header is available, fall back to the
    explicit ``cookie_secure`` flag.
    """
    explicit: bool = bool(getattr(request.app.state, "cookie_secure", False))
    peer = request.client.host if request.client else ""
    xfp = request.headers.get("X-Forwarded-Proto")
    trusted = request.app.state.trusted_proxies
    return trusted.forwarded_scheme(peer, xfp, "http") == "https" or explicit


@router.post("/auth")
async def auth(
    request: Request,
    payload: dict = Body(...),
) -> JSONResponse:
    """Verify password, mint a token, attach session cookie."""
    if not _origin_check_ok(request):
        logger.warning("Rejecting /api/auth from disallowed origin %r",
                       request.headers.get("origin"))
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

    auth_obj = get_authenticator()
    ip = _client_ip(request)
    try:
        auth_obj.check_rate_limit(ip)
    except RateLimitedError as e:
        return JSONResponse(
            {"error": "rate_limited", "retry_after": int(e.retry_after)},
            status_code=429,
        )

    password = ""
    totp_code: str | int | None = None
    if isinstance(payload, dict):
        pw = payload.get("password", "")
        if isinstance(pw, str):
            password = pw
        raw_totp = payload.get("totp", None)
        if isinstance(raw_totp, (str, int)):
            totp_code = raw_totp

    if not auth_obj.verify(password):
        auth_obj.record_failure(ip)
        logger.warning("Auth failure from %s (bad password)", ip)
        return JSONResponse({"error": "invalid_password"}, status_code=401)

    # TOTP second factor. Only checked after the password is correct, so a
    # wrong password alone never reaches the TOTP code path (avoids leaking
    # whether TOTP is required). The error is intentionally generic so an
    # attacker cannot tell apart "no TOTP given" from "wrong TOTP".
    if auth_obj.require_totp:
        if not auth_obj.verify_totp(totp_code):
            auth_obj.record_failure(ip)
            logger.warning("Auth failure from %s (bad TOTP)", ip)
            return JSONResponse({"error": "invalid_credentials"}, status_code=401)

    auth_obj.record_success(ip)
    token = auth_obj.generate_token()
    logger.info("Auth success from %s (token=%s)", ip, token_fingerprint(token))

    resp = JSONResponse({"ok": True})
    # Persistent cookie matching the token TTL. Logout button revokes both
    # cookie and token; idle reaper kills the PTY independently.
    set_session_cookie(
        resp,
        token,
        max_age=int(auth_obj.token_ttl),
        secure=_cookie_secure(request),
    )
    return resp


@router.post("/verify")
async def verify(request: Request) -> JSONResponse:
    """Return ``{ok: true|false}`` for the cookie's token; rate-limited per IP."""
    if not _origin_check_ok(request):
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

    auth_obj = get_authenticator()
    ip = _client_ip(request)
    if not auth_obj.check_verify_rate(ip):
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
        )

    token = read_session_cookie(request)
    ok = bool(token) and auth_obj.check_auth(token)  # type: ignore[arg-type]
    return JSONResponse({"ok": ok})


@router.get("/auth/mode")
async def auth_mode(request: Request) -> JSONResponse:
    """Return the current auth mode (which login factors are required).

    Used by the login form to know whether to render the TOTP field. This is
    a deployment-wide constant for any given server instance, so the response
    carries no per-user data; we still rate-limit it to the ``/verify`` bucket
    (shared sliding-window per IP) to prevent abuse.
    """
    if not _origin_check_ok(request):
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

    auth_obj = get_authenticator()
    ip = _client_ip(request)
    if not auth_obj.check_verify_rate(ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    return JSONResponse({"require_totp": auth_obj.require_totp})


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke the cookie's token, kill its sticky PTY, expire the cookie."""
    if not _origin_check_ok(request):
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

    auth_obj = get_authenticator()
    token = read_session_cookie(request)
    if token:
        auth_obj.revoke(token)
        registry: SessionRegistry | None = getattr(request.app.state, "registry", None)
        if registry is not None:
            registry.discard(token)
        logger.info("Logout for token=%s", token_fingerprint(token))

    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp, secure=_cookie_secure(request))
    return resp
