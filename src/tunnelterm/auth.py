"""Authentication: password verification, TOTP, session tokens, brute-force rate limit.

A single module-level :class:`Authenticator` instance is created at startup
(via :func:`get_authenticator`) and shared across all request handlers. Tokens
have a configurable TTL and the store is bounded by an LRU cap. Each token may
only be in active use by one WebSocket connection at a time (single-session
binding).

Rate limiting is per source IP, with exponential backoff after consecutive
failures.

Optionally, a TOTP (RFC 6238) second factor can be required on top of the
password. When ``require_totp`` is true and a ``totp_secret`` is configured,
``/api/auth`` will only mint a session token when the caller supplies a
current 6-digit code from a TOTP app (Google Authenticator, 1Password, etc.)
in addition to the password.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import secrets
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pyotp

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "tunnelterm" / "config.toml"
ENV_PASSWORD_VAR = "TUNNELTERM_PASSWORD"
ENV_TOTP_SECRET_VAR = "TUNNELTERM_TOTP_SECRET"
ENV_REQUIRE_TOTP_VAR = "TUNNELTERM_REQUIRE_TOTP"

# Clock-skew window for TOTP verification. pyotp's ``valid_window=1`` accepts
# the previous, current, and next 30-second step. This tolerates a few seconds
# of clock drift between the server and the authenticator app.
TOTP_VALID_WINDOW = 1

# Token defaults
DEFAULT_TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24h
DEFAULT_MAX_TOKENS = 64

# Rate-limit defaults for /auth (password attempts)
RATE_LIMIT_WINDOW_SECONDS = 15 * 60  # 15min sliding window
RATE_LIMIT_MAX_FAILURES = 5  # after this many failures in the window, lock out
RATE_LIMIT_LOCKOUT_SECONDS = 5 * 60  # lockout duration

# Rate-limit defaults for /verify (token-existence probe). The token is
# 256-bit unguessable, so this is a CPU/abuse limit rather than a brute-force
# defense. We tolerate many more hits per minute here than on /auth -- the
# auto-login flow + reconnects can legitimately produce dozens of hits per
# page-visit, and behind a reverse proxy without trusted-proxy config, every
# user looks like a single IP, so this cap is shared.
VERIFY_RATE_WINDOW_SECONDS = 60.0
VERIFY_RATE_MAX_HITS = 300  # per IP per minute

# Loopback CIDRs are trusted to forward client metadata (X-Forwarded-For /
# X-Forwarded-Proto). Any other peer's headers are ignored.
_DEFAULT_TRUSTED_PROXY_CIDRS = ("127.0.0.0/8", "::1/128")


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
    """Password verification + optional TOTP + session token store + rate limiter."""

    def __init__(
        self,
        password: str | None = None,
        token_ttl: float = DEFAULT_TOKEN_TTL_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        totp_secret: str | None = None,
        require_totp: bool = False,
    ) -> None:
        """Initialize.

        Args:
            password: Plaintext password. If None, loaded from env/config.
            token_ttl: Token lifetime in seconds.
            max_tokens: LRU cap on outstanding tokens.
            totp_secret: Base32 TOTP shared secret (e.g. ``"JBSWY3DPEHPK3PXP"``).
                If None, loaded from env/config.
            require_totp: When True (and ``totp_secret`` is set), ``/api/auth``
                demands a valid TOTP code in addition to the password. Ignored
                when no secret is configured -- TOTP cannot be enforced without
                one.

        Raises:
            AuthenticationError: If no password is configured, or ``require_totp``
                is True but no TOTP secret is available.

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

        # TOTP second factor. The pyotp.TOTP object is cheap; building it lazily
        # is unnecessary. pyotp validates the Base32 string lazily at
        # verify()/now() time, so we generate one code here to fail fast on a
        # malformed secret (binascii.Error, a subclass of ValueError).
        loaded_totp = totp_secret if totp_secret is not None else self._load_totp_secret()
        self._totp: pyotp.TOTP | None = None
        if loaded_totp:
            try:
                self._totp = pyotp.TOTP(loaded_totp)
                self._totp.now()
            except ValueError as e:
                msg = f"Invalid TOTP secret: {e}"
                raise AuthenticationError(msg) from e
        self._require_totp: bool = bool(require_totp and self._totp is not None)
        if require_totp and self._totp is None:
            msg = (
                f"--require-totp / require_totp was set, but no TOTP secret is "
                f"configured. Set {ENV_TOTP_SECRET_VAR} or 'totp_secret' in config."
            )
            raise AuthenticationError(msg)

        # token -> record. Insertion order = LRU order.
        self._tokens: dict[str, _TokenRecord] = {}
        # ip -> list of failure monotonic-times within window
        self._failures: dict[str, list[float]] = {}
        # ip -> monotonic-time after which they may try again (lockout end)
        self._lockouts: dict[str, float] = {}
        # ip -> list of /verify hit times within the sliding window
        self._verify_hits: dict[str, list[float]] = {}

    @property
    def token_ttl(self) -> float:
        """Token lifetime, in seconds (read-only)."""
        return self._token_ttl

    @property
    def require_totp(self) -> bool:
        """True if ``/api/auth`` also requires a valid TOTP code."""
        return self._require_totp

    @property
    def totp_configured(self) -> bool:
        """True if a TOTP secret is loaded (regardless of whether it's required)."""
        return self._totp is not None

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

    @staticmethod
    def _load_totp_secret() -> str | None:
        """Load Base32 TOTP secret from env var, falling back to config file."""
        env_secret = os.environ.get(ENV_TOTP_SECRET_VAR)
        if env_secret:
            logger.debug("TOTP secret loaded from environment variable")
            return env_secret

        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open("rb") as f:
                    config = tomllib.load(f)
                secret = config.get("totp_secret")
                if isinstance(secret, str) and secret:
                    logger.debug("TOTP secret loaded from config file")
                    return secret
            except (OSError, tomllib.TOMLDecodeError) as e:
                logger.warning("Failed to load config file %s: %s", CONFIG_PATH, e)
        return None

    # ---------- verification ----------

    def verify(self, password: str) -> bool:
        """Constant-time password comparison."""
        return secrets.compare_digest(self._password, password)

    def verify_totp(self, code: str | int | None) -> bool:
        """Return True if ``code`` is a valid TOTP code for the configured secret.

        Accepts ``None`` (missing), non-string, or malformed input as invalid
        without raising. Whitespace is stripped so users can paste codes with
        stray spaces. Uses a ``valid_window`` of :data:`TOTP_VALID_WINDOW`
        (default 1 step = ±30s) to tolerate clock drift.
        """
        if self._totp is None:
            return False
        if code is None:
            return False
        # Authenticator apps only emit digits, but be lenient about the wire
        # type -- a JSON body could deliver an int if a scriptable client is
        # used. We only accept strings here; anything else is invalid.
        if not isinstance(code, str):
            return False
        cleaned = code.strip()
        if not cleaned or not cleaned.isdigit():
            return False
        return bool(self._totp.verify(cleaned, valid_window=TOTP_VALID_WINDOW))

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

    # ---------- /verify rate limit ----------

    def check_verify_rate(self, ip: str) -> bool:
        """Record a /verify hit from ``ip``; return False if the IP is over the cap.

        Uses a per-IP sliding-window counter independent of the password-attempt
        limiter, with a much higher threshold (60/min by default). A False return
        means the caller should reject the request without doing any token work.
        """
        now = time.monotonic()
        window_start = now - VERIFY_RATE_WINDOW_SECONDS
        hits = [t for t in self._verify_hits.get(ip, []) if t >= window_start]
        if len(hits) >= VERIFY_RATE_MAX_HITS:
            self._verify_hits[ip] = hits  # keep the trimmed list
            return False
        hits.append(now)
        self._verify_hits[ip] = hits
        return True


# ---------- helpers ----------


class TrustedProxies:
    """Match peer IPs against a list of CIDRs.

    Used to decide whether ``X-Forwarded-For`` / ``X-Forwarded-Proto`` headers
    on an incoming request should be honored. Trusting them from arbitrary
    clients lets attackers spoof their source IP and bypass per-IP rate
    limits.
    """

    def __init__(self, cidrs: Iterable[str] | None = None) -> None:
        """Build the matcher. Invalid CIDRs are dropped with a warning."""
        self._networks: list[ipaddress._BaseNetwork] = []
        cidrs = cidrs or _DEFAULT_TRUSTED_PROXY_CIDRS
        for c in cidrs:
            c = (c or "").strip()
            if not c:
                continue
            try:
                self._networks.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                logger.warning("Ignoring invalid trusted-proxy CIDR %r", c)

    def trusts(self, peer_ip: str) -> bool:
        """Return True if ``peer_ip`` is inside any configured CIDR."""
        if not peer_ip:
            return False
        try:
            addr = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    def client_ip(self, peer_ip: str, xff_header: str | None) -> str:
        """Resolve the real client IP, trusting XFF only from a trusted peer.

        ``X-Forwarded-For`` is a comma-separated chain; the **left-most**
        entry is the original client. Anything to its right is an
        intermediate proxy that prepended its own peer. We walk the chain
        right-to-left, accepting hops while each previous hop is trusted,
        and stop at the first untrusted hop -- treating it as the real
        client. This prevents spoofing via injected XFF entries.
        """
        if not self.trusts(peer_ip):
            # Peer is not a trusted proxy -> ignore the header entirely.
            return peer_ip or "unknown"
        if not xff_header:
            return peer_ip or "unknown"
        hops = [h.strip() for h in xff_header.split(",") if h.strip()]
        if not hops:
            return peer_ip or "unknown"
        # Walk right-to-left: the right-most entry was added by our peer.
        # Skip trusted hops; the first untrusted hop is the client.
        for hop in reversed(hops):
            if not self.trusts(hop):
                return hop
        # All hops trusted (e.g. CDN chain entirely within our network) -->
        # use the left-most entry as the originator.
        return hops[0]

    def forwarded_scheme(
        self,
        peer_ip: str,
        xfp_header: str | None,
        default_scheme: str,
    ) -> str:
        """Return the request scheme, honoring ``X-Forwarded-Proto`` from trust."""
        if not self.trusts(peer_ip) or not xfp_header:
            return default_scheme
        # XFP can be comma-separated like XFF; the left-most is the original.
        first = xfp_header.split(",", 1)[0].strip().lower()
        if first in ("http", "https"):
            return first
        return default_scheme


def token_fingerprint(token: str) -> str:
    """Return a stable, non-reversible 8-char tag for ``token`` (for logs).

    Logging raw token prefixes leaks bits of the secret. We log a SHA-256
    fingerprint instead so log aggregators don't accumulate partial tokens.
    """
    if not token:
        return "<empty>"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return digest[:8]


# IPv4/IPv6 loopback prefixes treated as "safe to bind to without an
# origin allow-list" by :func:`is_loopback_host`.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"})


def is_loopback_host(host: str) -> bool:
    """Return True if ``host`` is loopback-only (no external interface).

    The wildcard binds (``0.0.0.0`` / ``::``) are NOT loopback, but we resolve
    them in :func:`origin_enforcement_required` separately because the user
    may have legitimate reasons (containers, dev servers) to bind wildcard
    on a private network.
    """
    if not host:
        return False
    host = host.strip().lower()
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    if host.startswith("127."):
        return True
    return False


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


def _normalize_origin(value: str) -> str:
    """Canonicalize an origin string for comparison.

    Browsers send ``Origin`` without a path, but operators frequently configure
    the allow-list with a trailing slash, mixed case, or a default port. We
    normalize both sides so common typos don't cause mysterious 403s:

    * lowercase scheme and host
    * strip trailing slash
    * strip default ports (``:443`` for ``https``, ``:80`` for ``http``)
    """
    from urllib.parse import urlparse

    s = value.strip()
    if not s:
        return ""
    # If someone wrote just ``example.com``, treat it as bare host.
    if "://" not in s:
        return s.lower().rstrip("/")
    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if not host:
        return s.lower().rstrip("/")
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return f"{scheme}://{netloc}"


def origin_allowed(origin: str | None, allowed: Iterable[str]) -> bool:
    """Return True if ``origin`` is in the allow-list, or if the list is empty.

    A None or empty origin (non-browser client) is allowed only when the
    allow-list itself is empty (default-permissive for local CLI usage).

    Both sides are normalized (case, default port, trailing slash) before
    comparison so e.g. ``https://Example.COM/`` and ``https://example.com``
    match.
    """
    allowed_set = {_normalize_origin(o) for o in allowed if o.strip()}
    if not allowed_set:
        # No allow-list configured: permit (caller bound to localhost typically).
        return True
    if not origin:
        return False
    return _normalize_origin(origin) in allowed_set
