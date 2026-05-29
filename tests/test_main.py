"""Unit tests for the FastAPI app factory + small helpers in the route layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tunnelterm.constants import CONTROL_KEY, MAX_WS_TEXT_FRAME_BYTES
from tunnelterm.main import create_app
from tunnelterm.middleware import SecurityHeadersMiddleware
from tunnelterm.routes.terminal_ws import _check_origin


class Test_check_origin:
    def test_empty_allow_list_permits_all(self) -> None:
        """Empty allow-list is default-permissive (loopback CLI use)."""
        ws = MagicMock()
        ws.headers.get.return_value = "https://evil.com"
        ws.app.state.allowed_origins = []
        ws.app.state.allow_any_origin = False
        assert _check_origin(ws) is True

    def test_matching_origin_permitted(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "https://terminal.example.com"
        ws.app.state.allowed_origins = ["https://terminal.example.com"]
        ws.app.state.allow_any_origin = False
        assert _check_origin(ws) is True

    def test_non_matching_origin_rejected(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "https://evil.example.com"
        ws.app.state.allowed_origins = ["https://terminal.example.com"]
        ws.app.state.allow_any_origin = False
        assert _check_origin(ws) is False

    def test_allow_any_origin_overrides_list(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "https://anything.com"
        ws.app.state.allowed_origins = ["https://terminal.example.com"]
        ws.app.state.allow_any_origin = True
        assert _check_origin(ws) is True


class TestSecurityHeadersMiddleware:
    @pytest.mark.asyncio
    async def test_csp_header_added(self) -> None:
        app = create_app(command="echo ok")
        mw = SecurityHeadersMiddleware(app)
        request = MagicMock()
        response = MagicMock()
        response.headers = {}
        call_next = AsyncMock(return_value=response)
        await mw.dispatch(request, call_next)
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        # Hardened CSP: no wildcard ws:/wss: source.
        assert "ws:" not in csp
        assert "wss:" not in csp

    @pytest.mark.asyncio
    async def test_security_headers_all_present(self) -> None:
        app = create_app(command="echo ok")
        mw = SecurityHeadersMiddleware(app)
        request = MagicMock()
        response = MagicMock()
        response.headers = {}
        call_next = AsyncMock(return_value=response)
        await mw.dispatch(request, call_next)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "geolocation=()" in response.headers["Permissions-Policy"]
        # HSTS is opt-in.
        assert "Strict-Transport-Security" not in response.headers

    @pytest.mark.asyncio
    async def test_hsts_emitted_when_enabled(self) -> None:
        app = create_app(command="echo ok", enable_hsts=True)
        mw = SecurityHeadersMiddleware(app, enable_hsts=True)
        request = MagicMock()
        response = MagicMock()
        response.headers = {}
        call_next = AsyncMock(return_value=response)
        await mw.dispatch(request, call_next)
        assert "max-age=" in response.headers["Strict-Transport-Security"]

    @pytest.mark.asyncio
    async def test_csp_not_duplicated_if_already_set(self) -> None:
        app = create_app(command="echo ok")
        mw = SecurityHeadersMiddleware(app)
        request = MagicMock()
        response = MagicMock()
        response.headers = {"Content-Security-Policy": "existing-value"}
        call_next = AsyncMock(return_value=response)
        await mw.dispatch(request, call_next)
        assert response.headers["Content-Security-Policy"] == "existing-value"


class Test_create_app:
    def test_app_has_correct_state(self) -> None:
        app = create_app(command="bash", allowed_origins=["https://a.com"])
        assert app.state.command == "bash"
        assert app.state.allowed_origins == ["https://a.com"]
        assert app.state.cookie_secure is False
        assert app.state.allow_any_origin is False

    def test_app_state_propagates_security_flags(self) -> None:
        app = create_app(
            command="bash",
            cookie_secure=True,
            allow_any_origin=True,
        )
        assert app.state.cookie_secure is True
        assert app.state.allow_any_origin is True

    def test_app_has_healthz_endpoint(self) -> None:
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/healthz" in routes

    def test_app_has_index_route(self) -> None:
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/" in routes

    def test_app_has_new_auth_routes(self) -> None:
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/api/auth" in routes
        assert "/api/verify" in routes
        assert "/api/logout" in routes
        assert "/ws" in routes

    def test_old_ws_auth_routes_are_gone(self) -> None:
        """The pre-v0.1.4 WebSocket auth surface must no longer exist."""
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/auth" not in routes
        assert "/verify" not in routes
        assert "/logout" not in routes

    def test_allowed_origins_default_empty(self) -> None:
        app = create_app(command="echo ok")
        assert app.state.allowed_origins == []


class Test_constants:
    def test_control_key_value(self) -> None:
        assert CONTROL_KEY == "__tt"

    def test_ws_frame_cap_sane(self) -> None:
        assert MAX_WS_TEXT_FRAME_BYTES >= 64 * 1024
        assert MAX_WS_TEXT_FRAME_BYTES <= 16 * 1024 * 1024
