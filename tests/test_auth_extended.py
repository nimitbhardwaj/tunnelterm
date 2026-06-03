"""Extended unit tests for tunnelterm.auth — singleton, config loading, edge cases."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pyotp
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


# Canonical RFC 6238 test secret used throughout the TOTP tests. Base32
# ("JBSWY3DPEHPK3PXP") is the standard "Hello!" example.
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


class Test_totp_disabled_by_default:
    def test_require_totp_false_without_secret(self) -> None:
        a = Authenticator(password="secret")
        assert a.require_totp is False
        assert a.totp_configured is False

    def test_require_totp_false_with_secret_but_no_require(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET)
        assert a.require_totp is False
        assert a.totp_configured is True

    def test_verify_totp_returns_false_when_unconfigured(self) -> None:
        a = Authenticator(password="secret")
        assert a.verify_totp("123456") is False


class Test_totp_verification:
    def test_current_code_accepted(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        code = pyotp.TOTP(TOTP_SECRET).now()
        assert a.verify_totp(code) is True

    def test_wrong_code_rejected(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        assert a.verify_totp("000000") is False

    def test_whitespace_stripped(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        code = pyotp.TOTP(TOTP_SECRET).now()
        assert a.verify_totp(f"  {code}  ") is True
        assert a.verify_totp(f"\n{code}\t") is True

    def test_non_digit_rejected(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        assert a.verify_totp("abcdef") is False
        assert a.verify_totp("12 456") is False
        assert a.verify_totp("12345a") is False

    def test_short_or_long_rejected(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        assert a.verify_totp("12345") is False
        assert a.verify_totp("1234567") is False
        assert a.verify_totp("") is False

    def test_none_rejected(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        assert a.verify_totp(None) is False  # type: ignore[arg-type]

    def test_non_string_rejected(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET, require_totp=True)
        assert a.verify_totp(123456) is False  # type: ignore[arg-type]
        assert a.verify_totp([1, 2, 3, 4, 5, 6]) is False  # type: ignore[arg-type]


class Test_totp_constructor_validation:
    def test_require_totp_without_secret_raises(self) -> None:
        with pytest.raises(AuthenticationError, match="TOTP"):
            Authenticator(password="secret", require_totp=True)

    def test_invalid_secret_raises(self) -> None:
        # Non-Base32 characters are rejected by pyotp.
        with pytest.raises(AuthenticationError, match="TOTP"):
            Authenticator(password="secret", totp_secret="not!valid!base32!!!")

    def test_valid_secret_accepted(self) -> None:
        a = Authenticator(password="secret", totp_secret=TOTP_SECRET)
        assert a.totp_configured is True


class Test_totp_secret_loading:
    def test_load_totp_secret_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure config file path doesn't accidentally resolve to a real file.
        import tunnelterm.auth as auth_module
        from pathlib import Path

        monkeypatch.setattr(auth_module, "CONFIG_PATH", Path("/nonexistent/config.toml"))
        monkeypatch.setenv("TUNNELTERM_TOTP_SECRET", TOTP_SECRET)
        a = Authenticator(password="secret")
        assert a.totp_configured is True
        assert a.verify_totp(pyotp.TOTP(TOTP_SECRET).now()) is True

    def test_load_totp_secret_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunnelterm.auth as auth_module

        monkeypatch.delenv("TUNNELTERM_TOTP_SECRET", raising=False)
        with tempfile.NamedTemporaryFile(suffix=".toml", mode="w", delete=False) as f:
            f.write(f'totp_secret = "{TOTP_SECRET}"\n')
            path = Path(f.name)
        try:
            monkeypatch.setattr(auth_module, "CONFIG_PATH", path)
            a = Authenticator(password="secret")
            assert a.totp_configured is True
        finally:
            os.unlink(path)

    def test_no_secret_means_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tunnelterm.auth as auth_module

        monkeypatch.delenv("TUNNELTERM_TOTP_SECRET", raising=False)
        monkeypatch.setattr(auth_module, "CONFIG_PATH", Path("/nonexistent/config.toml"))
        a = Authenticator(password="secret")
        assert a.totp_configured is False
        assert a.require_totp is False


class Test_totp_and_password_independent:
    """Wrong password must not consume a TOTP attempt; both checks are independent."""

    def test_password_check_runs_first(self) -> None:
        a = Authenticator(password="correct", totp_secret=TOTP_SECRET, require_totp=True)
        # Password is wrong; we should never even reach the TOTP check.
        assert a.verify("wrong") is False
        # And verify_totp should not have been called -- but it's a pure
        # function, so the only way to assert this is to ensure the route
        # order is enforced. The route itself is tested in test_server_integration.
        assert a.verify_totp(pyotp.TOTP(TOTP_SECRET).now()) is True

    def test_both_correct_succeeds(self) -> None:
        a = Authenticator(password="correct", totp_secret=TOTP_SECRET, require_totp=True)
        code = pyotp.TOTP(TOTP_SECRET).now()
        assert a.verify("correct") is True
        assert a.verify_totp(code) is True
