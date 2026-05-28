"""Unit tests for tunnelterm.main — FastAPI app factory and helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tunnelterm.main import (
    CONTROL_KEY,
    WS_SUBPROTOCOL,
    SecurityHeadersMiddleware,
    _check_origin,
    _extract_token,
    create_app,
)


class Test_extract_token:
    def test_no_header_returns_none(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = ""
        assert _extract_token(ws) is None

    def test_missing_subprotocol_returns_none(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "tunnelterm.v1.token"
        assert _extract_token(ws) is None

    def test_token_extracted_from_subprotocols(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "tunnelterm.v1.token, mytoken123"
        assert _extract_token(ws) == "mytoken123"

    def test_token_first_in_list(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "mytoken456, tunnelterm.v1.token"
        assert _extract_token(ws) == "mytoken456"

    def test_whitespace_trimmed(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "tunnelterm.v1.token,  token_with_spaces  "
        assert _extract_token(ws) == "token_with_spaces"


class Test_check_origin:
    def test_empty_allow_list_permits_all(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "https://evil.com"
        ws.app.state.allowed_origins = []
        assert _check_origin(ws) is True

    def test_matching_origin_permitted(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "https://terminal.example.com"
        ws.app.state.allowed_origins = ["https://terminal.example.com"]
        assert _check_origin(ws) is True

    def test_non_matching_origin_rejected(self) -> None:
        ws = MagicMock()
        ws.headers.get.return_value = "https://evil.example.com"
        ws.app.state.allowed_origins = ["https://terminal.example.com"]
        assert _check_origin(ws) is False


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
        assert "Content-Security-Policy" in response.headers
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]

    @pytest.mark.asyncio
    async def test_security_headers_all_present(self) -> None:
        app = create_app(command="echo ok")
        mw = SecurityHeadersMiddleware(app)
        request = MagicMock()
        response = MagicMock()
        response.headers = {}
        call_next = AsyncMock(return_value=response)
        await mw.dispatch(request, call_next)
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "no-referrer"
        assert "geolocation=()" in response.headers.get("Permissions-Policy", "")

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

    def test_app_has_healthz_endpoint(self) -> None:
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/healthz" in routes

    def test_app_has_index_route(self) -> None:
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/" in routes

    def test_app_has_ws_routes(self) -> None:
        app = create_app(command="echo ok")
        routes = [r.path for r in app.routes]
        assert "/ws" in routes
        assert "/auth" in routes
        assert "/logout" in routes

    def test_allowed_origins_default_empty(self) -> None:
        app = create_app(command="echo ok")
        assert app.state.allowed_origins == []


class Test_constants:
    def test_control_key_is_defined(self) -> None:
        assert CONTROL_KEY == "__tt"

    def test_ws_subprotocol_format(self) -> None:
        assert WS_SUBPROTOCOL == "tunnelterm.v1.token"