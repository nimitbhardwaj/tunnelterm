"""Unit tests for the Authenticator class."""

from __future__ import annotations

import pytest

from tunnelterm.auth import (
    Authenticator,
    AuthenticationError,
    RateLimitedError,
    origin_allowed,
)


def test_init_requires_password() -> None:
    """Authenticator with no password raises."""
    with pytest.raises(AuthenticationError):
        Authenticator(password="")


def test_verify_correct_password() -> None:
    """Right password verifies, wrong does not."""
    a = Authenticator(password="secret")
    assert a.verify("secret") is True
    assert a.verify("wrong") is False
    assert a.verify("") is False


def test_token_lifecycle() -> None:
    """Issue, validate, revoke."""
    a = Authenticator(password="secret")
    t = a.generate_token()
    assert a.check_auth(t) is True
    a.revoke(t)
    assert a.check_auth(t) is False


def test_token_ttl_expiry() -> None:
    """A token past its TTL is no longer valid."""
    a = Authenticator(password="secret", token_ttl=0.05)
    t = a.generate_token()
    assert a.check_auth(t) is True
    import time

    time.sleep(0.1)
    assert a.check_auth(t) is False


def test_token_lru_eviction() -> None:
    """When token store is full, oldest is evicted."""
    a = Authenticator(password="secret", max_tokens=3)
    tokens = [a.generate_token() for _ in range(3)]
    assert all(a.check_auth(t) for t in tokens)
    new = a.generate_token()
    # The oldest of the original three is gone.
    assert a.check_auth(tokens[0]) is False
    assert a.check_auth(tokens[1]) is True
    assert a.check_auth(tokens[2]) is True
    assert a.check_auth(new) is True


def test_single_active_session() -> None:
    """try_acquire_session prevents double-use."""
    a = Authenticator(password="secret")
    t = a.generate_token()
    assert a.try_acquire_session(t) is True
    assert a.try_acquire_session(t) is False  # already in use
    a.release_session(t)
    assert a.try_acquire_session(t) is True


def test_rate_limit_lockout() -> None:
    """N consecutive failures lock the IP out."""
    a = Authenticator(password="secret")
    ip = "1.2.3.4"
    # First few failures: no lockout.
    for _ in range(4):
        a.check_rate_limit(ip)
        a.record_failure(ip)
    # Fifth failure crosses the threshold.
    a.record_failure(ip)
    with pytest.raises(RateLimitedError):
        a.check_rate_limit(ip)


def test_rate_limit_success_clears() -> None:
    """A successful auth clears prior failures."""
    a = Authenticator(password="secret")
    ip = "9.9.9.9"
    for _ in range(3):
        a.record_failure(ip)
    a.record_success(ip)
    # Should not raise.
    a.check_rate_limit(ip)


def test_origin_allowlist_empty_permits_all() -> None:
    """Empty allow-list defaults open."""
    assert origin_allowed("https://anything", []) is True
    assert origin_allowed(None, []) is True


def test_origin_allowlist_blocks_unknown() -> None:
    """Non-empty allow-list rejects unknown origins."""
    allowed = ["https://terminal.example.com"]
    assert origin_allowed("https://terminal.example.com", allowed) is True
    assert origin_allowed("https://evil.example.com", allowed) is False
    assert origin_allowed(None, allowed) is False


def test_origin_normalization_trailing_slash() -> None:
    """Trailing slash on allow-list entry is ignored."""
    allowed = ["https://terminal.example.com/"]
    assert origin_allowed("https://terminal.example.com", allowed) is True
    assert origin_allowed("https://terminal.example.com/", allowed) is True


def test_origin_normalization_case() -> None:
    """Scheme and host are case-insensitive."""
    allowed = ["https://Terminal.EXAMPLE.COM"]
    assert origin_allowed("https://terminal.example.com", allowed) is True
    assert origin_allowed("HTTPS://TERMINAL.EXAMPLE.COM", allowed) is True


def test_origin_normalization_default_port() -> None:
    """Default ports are stripped before comparison."""
    allowed = ["https://terminal.example.com"]
    assert origin_allowed("https://terminal.example.com:443", allowed) is True
    assert origin_allowed("http://terminal.example.com:80", allowed) is False  # wrong scheme
    allowed_http = ["http://terminal.example.com"]
    assert origin_allowed("http://terminal.example.com:80", allowed_http) is True


def test_origin_bare_hostname() -> None:
    """Bare hostname (no scheme) is accepted and lowercased."""
    from tunnelterm.auth import _normalize_origin
    assert _normalize_origin("Example.COM") == "example.com"
    assert _normalize_origin("Example.COM/") == "example.com"


from tunnelterm.auth import TrustedProxies


class TestTrustedProxies:
    def test_untrusted_peer_ignores_xff(self) -> None:
        """XFF from an untrusted peer is ignored."""
        tp = TrustedProxies(["127.0.0.0/8"])
        # Public IP is not in loopback -> XFF ignored
        assert tp.client_ip("8.8.8.8", "1.2.3.4") == "8.8.8.8"
        assert tp.client_ip("8.8.8.8", None) == "8.8.8.8"

    def test_trusted_peer_accepts_xff(self) -> None:
        """XFF from a trusted loopback peer is used; right-most untrusted is client."""
        tp = TrustedProxies(["127.0.0.0/8"])
        # Single entry -> it's the client
        assert tp.client_ip("127.0.0.1", "1.2.3.4") == "1.2.3.4"
        # Walking right-to-left: 5.6.7.8 is first untrusted -> it's the client
        assert tp.client_ip("127.0.0.1", "1.2.3.4, 5.6.7.8") == "5.6.7.8"

    def test_trusted_peer_chain(self) -> None:
        """Walk right-to-left, stop at first untrusted hop."""
        tp = TrustedProxies(["127.0.0.0/8"])
        # 5.6.7.8 is first untrusted going right-to-left -> it's the client
        assert tp.client_ip("127.0.0.1", "10.0.0.1, 5.6.7.8") == "5.6.7.8"
        # All hops trusted -> return left-most (originator)
        assert tp.client_ip("127.0.0.1", "10.0.0.1, 127.0.0.2") == "10.0.0.1"

    def test_forwarded_scheme_trusted(self) -> None:
        """X-Forwarded-Proto from trusted peer is honoured."""
        tp = TrustedProxies(["127.0.0.0/8"])
        assert tp.forwarded_scheme("127.0.0.1", "https", "http") == "https"
        assert tp.forwarded_scheme("127.0.0.1", "https, http", "http") == "https"

    def test_forwarded_scheme_untrusted(self) -> None:
        """X-Forwarded-Proto from untrusted peer is ignored."""
        tp = TrustedProxies(["127.0.0.0/8"])
        assert tp.forwarded_scheme("8.8.8.8", "https", "http") == "http"
        assert tp.forwarded_scheme("8.8.8.8", None, "http") == "http"
