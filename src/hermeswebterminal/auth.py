"""Authentication Module for password-based access control."""

from __future__ import annotations

import logging
import os
import secrets
import tomllib
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".config" / "hermes-web-terminal" / "config.toml"
ENV_PASSWORD_VAR = "HERMES_WEB_TERMINAL_PASSWORD"


class AuthenticationError(Exception):
    """Raised when authentication fails due to configuration issues."""


class Authenticator:
    """Handles password verification for terminal access."""

    _tokens: ClassVar[set[str]] = set()

    def __init__(self, password: str | None = None) -> None:
        """Initialize the authenticator.

        Args:
            password: The password to verify against. If None, loads from
                environment or config file.

        Raises:
            AuthenticationError: If no password is configured.

        """
        self._password: str
        loaded_password = password or self._load_password()
        if loaded_password is None:
            msg = "No password configured. Set HERMES_WEB_TERMINAL_PASSWORD env var or password in config file."
            raise AuthenticationError(msg)
        self._password = loaded_password

    def _load_password(self) -> str | None:
        """Load password from environment variable or config file.

        Environment variable takes precedence over config file.

        Returns:
            The configured password, or None if not set.

        """
        env_password = os.environ.get(ENV_PASSWORD_VAR)
        if env_password:
            logger.debug("Password loaded from environment variable")
            return env_password

        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open("rb") as f:
                    config = tomllib.load(f)
                password = config.get("password")
                if password:
                    logger.debug("Password loaded from config file")
                    return password
            except Exception as e:
                logger.warning(f"Failed to load config file: {e}")

        logger.debug("No password configured")
        return None

    def verify(self, password: str) -> bool:
        """Verify a password against the configured password.

        Args:
            password: The password to verify.

        Returns:
            True if the password matches, False otherwise.

        """
        is_valid = secrets.compare_digest(self._password, password)
        logger.debug(f"Password verification: {'success' if is_valid else 'failure'}")
        return is_valid

    def generate_token(self) -> str:
        """Generate a session token.

        Returns:
            A URL-safe session token.

        """
        token = secrets.token_urlsafe()
        self._tokens.add(token)
        logger.debug(f"Generated session token: {token[:8]}...")
        return token

    def check_auth(self, token: str) -> bool:
        """Check if a session token is valid.

        Args:
            token: The session token to validate.

        Returns:
            True if the token is valid, False otherwise.

        """
        is_valid = token in self._tokens
        logger.debug(f"Token validation: {'success' if is_valid else 'failure'}")
        return is_valid


def load_config(config_path: Path | None = None) -> dict:
    """Load configuration from TOML file.

    Args:
        config_path: Optional custom config path. Defaults to standard location.

    Returns:
        Configuration dictionary.

    """
    path = config_path or CONFIG_PATH
    if not path.exists():
        return {}

    with path.open("rb") as f:
        return tomllib.load(f)
