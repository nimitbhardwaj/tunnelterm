"""Session cookie helpers.

We store the auth token in an ``HttpOnly; Secure; SameSite=Strict`` cookie so
that JavaScript (and therefore XSS) cannot read it. The cookie also rides on
the WebSocket handshake automatically, removing the need for the client to
juggle the token at all.

Two cookie lifetimes:

* "Remember me" -> ``Max-Age = token_ttl`` (24h by default), persistent.
* Otherwise    -> no ``Max-Age``, session cookie scoped to the browser tab/window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request, Response

if TYPE_CHECKING:
    from fastapi import WebSocket

#: Name of the cookie carrying the session token.
COOKIE_NAME = "tt_session"
#: Path the cookie applies to.
COOKIE_PATH = "/"


def set_session_cookie(
    response: Response,
    token: str,
    *,
    max_age: int | None,
    secure: bool,
) -> None:
    """Attach the session cookie to ``response``.

    Args:
        response: The outbound response to attach a ``Set-Cookie`` header to.
        token: The opaque session token to store.
        max_age: Cookie lifetime in seconds, or ``None`` for a session cookie.
        secure: Whether to set the ``Secure`` flag. Should be ``True`` for any
            non-loopback deployment; ``False`` allows the cookie to be sent over
            plain ``http://`` (only acceptable on ``127.0.0.1``).

    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    """Expire the session cookie on the client."""
    response.set_cookie(
        key=COOKIE_NAME,
        value="",
        max_age=0,
        path=COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def read_session_cookie(request: Request) -> str | None:
    """Return the token from the request's cookies, or ``None`` if absent."""
    value = request.cookies.get(COOKIE_NAME)
    return value or None


def read_ws_cookie(ws: WebSocket) -> str | None:
    """Pull the session cookie out of a WebSocket handshake.

    Starlette's :class:`~starlette.websockets.WebSocket` exposes ``.cookies``
    as a plain dict on the request scope.
    """
    cookies = getattr(ws, "cookies", None)
    if isinstance(cookies, dict):
        value = cookies.get(COOKIE_NAME)
        if value:
            return value
    # Fallback: parse the raw Cookie header (some test clients don't fill
    # ws.cookies). Headers are case-insensitive.
    raw = ws.headers.get("cookie") or ws.headers.get("Cookie") or ""
    if not raw:
        return None
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value
    return None
