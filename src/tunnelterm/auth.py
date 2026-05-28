"""Authentication: password verification, session tokens, brute-force rate limit.

A single module-level :class:`Authenticator` instance is created at startup
(via :func:`get_authenticator`) and shared across all request handlers. Tokens
have a configurable TTL and the store is bounded by an LRU cap. Each token may
only be in active use by one WebSocket connection at a time (single-session
binding).

Rate limiting is per source IP, with exponential backoff after consecutive
failures.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "tunnelterm" / "config.toml"
ENV_PASSWORD_VAR = "TUNNELTERM_PASSWORD"

# Token defaults
DEFAULT_TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24h
DEFAULT_MAX_TOKENS = 64

# Rate-limit defaults
RATE_LIMIT_WINDOW_SECONDS = 15 * 60  # 15min sliding window
RATE_LIMIT_MAX_FAILURES = 5  # after this many failures in the window, lock out
RATE_LIMIT_LOCKOUT_SECONDS = 5 * 60  # lockout duration


class AuthenticationError(Exception):
    """Raised when authentication cannot be configured (no password)."""


class RateLimitedError(Exception):
    """Raised when a source IP has exceeded its allowed failure rate."""

    def __init__(self, retry_after: float) -> None:
        """Initialize.

        Args:
            retry_after: Seconds the caller must wait before retrying.

        """
        super().__init__(f"Rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class _TokenRecord:
    """Internal: tracks a token's expiry and active session state."""

    __slots__ = ("created_at", "expires_at", "in_use")

    def __init__(self, ttl: float) -> None:
        """Initialize a token record."""
        now = time.monotonic()
        self.created_at = now
        self.expires_at = now + ttl
        self.in_use = False


class Authenticator:
    """Password verification + session token store + rate limiter."""

    def __init__(
        self,
        password: str | None = None,
        token_ttl: float = DEFAULT_TOKEN_TTL_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        """Initialize.

        Args:
            password: Plaintext password. If None, loaded from env/config.
            token_ttl: Token lifetime in seconds.
            max_tokens: LRU cap on outstanding tokens.

        Raises:
            AuthenticationError: If no password is configured.

        """
        loaded = password if password is not None else self._load_password()
        if not loaded:
            msg = (
                f"No password configured. Set {ENV_PASSWORD_VAR} env var "
                "or 'password' in config file."
            )
            raise AuthenticationError(msg)
        self._password: str = loaded
        self._token_ttl: float = token_ttl
        self._max_tokens: int = max_tokens
        # token -> record. Insertion order = LRU order.
        self._tokens: dict[str, _TokenRecord] = {}
        # ip -> list of failure monotonic-times within window
        self._failures: dict[str, list[float]] = {}
        # ip -> monotonic-time after which they may try again (lockout end)
        self._lockouts: dict[str, float] = {}

    # ---------- password loading ----------

    @staticmethod
    def _load_password() -> str | None:
        """Load password from env var, falling back to config file."""
        env_password = os.environ.get(ENV_PASSWORD_VAR)
        if env_password:
            logger.debug("Password loaded from environment variable")
            return env_password

        if CONFIG_PATH.exists():
            try:
                _warn_if_world_readable(CONFIG_PATH)
                with CONFIG_PATH.open("rb") as f:
                    config = tomllib.load(f)
                password = config.get("password")
                if password:
                    logger.debug("Password loaded from config file")
                    return password
            except (OSError, tomllib.TOMLDecodeError) as e:
                logger.warning("Failed to load config file %s: %s", CONFIG_PATH, e)
        return None

    # ---------- verification ----------

    def verify(self, password: str) -> bool:
        """Constant-time password comparison."""
        return secrets.compare_digest(self._password, password)

    # ---------- token store ----------

    def generate_token(self) -> str:
        """Mint a new session token and add it to the store."""
        self._evict_expired()
        # LRU evict if at cap
        while len(self._tokens) >= self._max_tokens:
            oldest = next(iter(self._tokens))
            logger.debug("Token LRU evict: %s...", oldest[:8])
            self._tokens.pop(oldest, None)
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _TokenRecord(self._token_ttl)
        logger.debug("Issued token %s... (total=%d)", token[:8], len(self._tokens))
        return token

    def check_auth(self, token: str) -> bool:
        """Return True if ``token`` is currently valid (issued, unexpired)."""
        if not token:
            return False
        record = self._tokens.get(token)
        if record is None:
            return False
        if time.monotonic() >= record.expires_at:
            self._tokens.pop(token, None)
            return False
        return True

    def try_acquire_session(self, token: str) -> bool:
        """Bind a token to one active session. Returns False if already in use."""
        record = self._tokens.get(token)
        if record is None or time.monotonic() >= record.expires_at:
            return False
        if record.in_use:
            return False
        record.in_use = True
        return True

    def release_session(self, token: str) -> None:
        """Release the active-session lock on a token (still valid for reconnect)."""
        record = self._tokens.get(token)
        if record is not None:
            record.in_use = False

    def revoke(self, token: str) -> None:
        """Invalidate a token (logout)."""
        self._tokens.pop(token, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [t for t, r in self._tokens.items() if now >= r.expires_at]
        for t in expired:
            self._tokens.pop(t, None)

    # ---------- rate limiting ----------

    def check_rate_limit(self, ip: str) -> None:
        """Raise :class:`RateLimitedError` if ``ip`` is currently locked out."""
        now = time.monotonic()
        lockout_until = self._lockouts.get(ip)
        if lockout_until is not None:
            if now < lockout_until:
                raise RateLimitedError(lockout_until - now)
            # Lockout expired; reset.
            self._lockouts.pop(ip, None)
            self._failures.pop(ip, None)

    def record_failure(self, ip: str) -> None:
        """Record a failed auth from ``ip`` and lock out if threshold exceeded."""
        now = time.monotonic()
        window_start = now - RATE_LIMIT_WINDOW_SECONDS
        failures = [t for t in self._failures.get(ip, []) if t >= window_start]
        failures.append(now)
        self._failures[ip] = failures
        if len(failures) >= RATE_LIMIT_MAX_FAILURES:
            self._lockouts[ip] = now + RATE_LIMIT_LOCKOUT_SECONDS
            logger.warning(
                "IP %s locked out for %ds after %d failures",
                ip,
                RATE_LIMIT_LOCKOUT_SECONDS,
                len(failures),
            )

    def record_success(self, ip: str) -> None:
        """Clear an IP's failure history on a successful auth."""
        self._failures.pop(ip, None)
        self._lockouts.pop(ip, None)


# ---------- helpers ----------


def _warn_if_world_readable(path: Path) -> None:
    """Log a warning if ``path`` has group or other read permissions."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "Config file %s has loose permissions (mode=%o); "
            "recommend chmod 600",
            path,
            mode & 0o777,
        )


def load_config(config_path: Path | None = None) -> dict:
    """Load TOML configuration from the standard location (or override)."""
    path = config_path or CONFIG_PATH
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# ---------- module-level singleton ----------

_INSTANCE: Authenticator | None = None


def set_authenticator(authenticator: Authenticator) -> None:
    """Install the process-wide Authenticator (called once at startup)."""
    global _INSTANCE
    _INSTANCE = authenticator


def get_authenticator() -> Authenticator:
    """Return the process-wide Authenticator. Raises if not initialized."""
    if _INSTANCE is None:
        msg = "Authenticator not initialized; call set_authenticator() first"
        raise RuntimeError(msg)
    return _INSTANCE


def origin_allowed(origin: str | None, allowed: Iterable[str]) -> bool:
    """Return True if ``origin`` is in the allow-list, or if the list is empty.

    A None or empty origin (non-browser client) is allowed only when the
    allow-list itself is empty (default-permissive for local CLI usage).
    """
    allowed_set = {o.strip() for o in allowed if o.strip()}
    if not allowed_set:
        # No allow-list configured: permit (caller bound to localhost typically).
        return True
    if not origin:
        return False
    return origin in allowed_set
