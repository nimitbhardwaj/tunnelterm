"""Extended unit tests for tunnelterm.auth — singleton, config loading, edge cases."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tunnelterm.auth import (
    RATE_LIMIT_LOCKOUT_SECONDS,
    AuthenticationError,
    Authenticator,
    RateLimitedError,
    _warn_if_world_readable,
    get_authenticator,
    load_config,
    origin_allowed,
    set_authenticator,
)


class Test_authenticator_singleton:
    def test_get_authenticator_raises_when_uninitialized(self) -> None:
        # Reset singleton to ensure clean state
        import tunnelterm.auth as auth_module

        auth_module._INSTANCE = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                get_authenticator()
        finally:
            # Restore default so other tests aren't affected
            set_authenticator(Authenticator(password="test"))

    def test_set_and_get_round_trip(self) -> None:
        auth = Authenticator(password="roundtrip")
        set_authenticator(auth)
        assert get_authenticator() is auth


class Test_load_config:
    def test_missing_file_returns_empty_dict(self) -> None:
        result = load_config(Path("/nonexistent/path/config.toml"))
        assert result == {}

    def test_valid_toml_file(self) -> None:
        import tomllib

        with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
            f.write('password = "fromfile"\nport = 4200\n')
            path = Path(f.name)
        try:
            result = load_config(path)
            assert result["password"] == "fromfile"
            assert result["port"] == 4200
        finally:
            os.unlink(path)

    def test_invalid_toml_raises(self) -> None:
        import tomllib

        with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
            f.write("this is not valid toml {[[\n")
            path = Path(f.name)
        try:
            with pytest.raises(tomllib.TOMLDecodeError):
                load_config(path)
        finally:
            os.unlink(path)


class Test_warn_if_world_readable:
    def test_readable_file_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            path = Path(f.name)
        try:
            # Make world-readable
            os.chmod(path, 0o644)
            _warn_if_world_readable(path)
            assert "loose permissions" in caplog.text
        finally:
            os.unlink(path)

    def test_restricted_file_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            path = Path(f.name)
        try:
            os.chmod(path, 0o600)
            _warn_if_world_readable(path)
            assert "loose permissions" not in caplog.text
        finally:
            os.unlink(path)

    def test_missing_file_no_crash(self, caplog: pytest.LogCaptureFixture) -> None:
        _warn_if_world_readable(Path("/nonexistent/file"))
        assert "loose permissions" not in caplog.text


class Test_origin_allowed:
    def test_whitespace_in_allow_list(self) -> None:
        allowed = [" https://a.com ", "  https://b.com"]
        assert origin_allowed("https://a.com", allowed) is True
        assert origin_allowed("https://b.com", allowed) is True

    def test_empty_string_in_allow_list_ignored(self) -> None:
        allowed = ["", "https://valid.com", ""]
        assert origin_allowed("https://valid.com", allowed) is True
        assert origin_allowed("", allowed) is False

    def test_no_origin_non_empty_allowlist_rejected(self) -> None:
        assert origin_allowed(None, ["https://example.com"]) is False

    def test_no_origin_empty_allowlist_permitted(self) -> None:
        assert origin_allowed(None, []) is True
        assert origin_allowed("", []) is True


class Test_authenticator_evict_expired:
    def test_expired_tokens_evicted_on_generate(self) -> None:
        import time

        a = Authenticator(password="secret", token_ttl=0.2, max_tokens=10)
        t1 = a.generate_token()
        t2 = a.generate_token()
        assert a.check_auth(t1) is True
        assert a.check_auth(t2) is True
        time.sleep(0.1)  # t1 and t2 both have 0.2s TTL, still valid
        t3 = a.generate_token()
        assert a.check_auth(t1) is True
        assert a.check_auth(t2) is True
        assert a.check_auth(t3) is True

    def test_check_auth_evicts_expired(self) -> None:
        import time

        a = Authenticator(password="secret", token_ttl=0.05)
        t = a.generate_token()
        time.sleep(0.1)
        assert a.check_auth(t) is False

    def test_generate_triggers_eviction_of_expired_tokens(self) -> None:
        import time

        a = Authenticator(password="secret", token_ttl=0.05, max_tokens=2)
        t1 = a.generate_token()
        t2 = a.generate_token()
        time.sleep(0.1)  # Both expired
        t3 = a.generate_token()  # Should evict both expired, add t3
        assert a.check_auth(t1) is False
        assert a.check_auth(t2) is False
        assert a.check_auth(t3) is True


class Test_authenticator_generate_token:
    def test_generate_token_returns_urlsafe_string(self) -> None:
        a = Authenticator(password="secret")
        token = a.generate_token()
        assert len(token) >= 32
        # URL-safe base64 characters only
        import re

        assert re.match(r"^[A-Za-z0-9_-]+$", token)

    def test_generate_multiple_unique_tokens(self) -> None:
        a = Authenticator(password="secret")
        tokens = [a.generate_token() for _ in range(10)]
        assert len(set(tokens)) == 10


class Test_authenticator_release_session:
    def test_release_session_idempotent(self) -> None:
        a = Authenticator(password="secret")
        t = a.generate_token()
        a.try_acquire_session(t)
        a.release_session(t)
        a.release_session(t)  # Should not raise
        assert a.try_acquire_session(t) is True


class Test_authenticator_revoke:
    def test_revoke_nonexistent_token_no_error(self) -> None:
        a = Authenticator(password="secret")
        a.revoke("nonexistent-token")  # Should not raise

    def test_revoke_clears_in_use_flag(self) -> None:
        a = Authenticator(password="secret")
        t = a.generate_token()
        a.try_acquire_session(t)
        assert a.check_auth(t) is True
        a.revoke(t)
        assert a.check_auth(t) is False


class Test_rate_limit_lockout_expiry:
    def test_lockout_duration_config(self) -> None:
        assert RATE_LIMIT_LOCKOUT_SECONDS == 300

    def test_lockout_set_on_consecutive_failures(self) -> None:
        a = Authenticator(password="secret")
        ip = "5.6.7.8"
        for _ in range(5):
            a.record_failure(ip)
        with pytest.raises(RateLimitedError):
            a.check_rate_limit(ip)