"""CLI entrypoint for tunnelterm."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from tunnelterm import __version__
from tunnelterm.auth import (
    CONFIG_PATH,
    ENV_PASSWORD_VAR,
    AuthenticationError,
    Authenticator,
    load_config,
    set_authenticator,
)
from tunnelterm.session import DEFAULT_IDLE_TIMEOUT_SECONDS

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4200


def main() -> None:
    """Run the CLI."""
    parser = argparse.ArgumentParser(
        prog="tunnelterm",
        description="Web-based terminal: run any command in a PTY, accessed via browser.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", default=None, help=f"Host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"Port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--command",
        default=None,
        help=(
            "Command to run in the PTY (required). May include arguments, "
            'e.g. --command "bash -l".'
        ),
    )
    parser.add_argument(
        "--password-env",
        default=None,
        dest="password_env",
        help=f"Env var to read password from (default: {ENV_PASSWORD_VAR})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Config TOML path (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        dest="allowed_origins",
        default=None,
        help=(
            "Allowed Origin header value for WebSocket handshake. "
            "Repeat to allow multiple. If unset, all origins accepted."
        ),
    )
    parser.add_argument(
        "--session-idle-timeout",
        type=float,
        default=None,
        dest="session_idle_timeout",
        help=(
            "Seconds to keep a sticky PTY session alive after its last "
            "WebSocket disconnect, before the reaper kills the shell. "
            f"Default: {int(DEFAULT_IDLE_TIMEOUT_SECONDS)}s (5 hours)."
        ),
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        default=None,
        help="Log level (DEBUG, INFO, WARNING, ERROR)",
    )
    args = parser.parse_args()

    log_level_str = (args.log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    if not isinstance(log_level, int):
        log_level = logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    config = load_config(args.config)
    host = args.host or os.environ.get("TUNNELTERM_HOST") or config.get("host", DEFAULT_HOST)
    port_str = os.environ.get("TUNNELTERM_PORT")
    port = (
        args.port
        if args.port is not None
        else (int(port_str) if port_str else config.get("port", DEFAULT_PORT))
    )
    command = args.command or os.environ.get("TUNNELTERM_COMMAND") or config.get("command")
    if not command:
        parser.error(
            "no command specified. Use --command, set TUNNELTERM_COMMAND, "
            "or add 'command = \"...\"' to the config file."
        )

    # Resolve password.
    password_env = args.password_env or ENV_PASSWORD_VAR
    password = os.environ.get(password_env) or config.get("password")
    if not password:
        logger.error(
            "no password configured: set %s or add 'password' to %s",
            password_env,
            CONFIG_PATH,
        )
        sys.exit(2)
    # Re-export to canonical env var so Authenticator (env path) sees it.
    os.environ[ENV_PASSWORD_VAR] = password

    # Allowed origins
    allowed_origins_raw: list[str] = []
    if args.allowed_origins:
        allowed_origins_raw.extend(args.allowed_origins)
    env_origins = os.environ.get("TUNNELTERM_ALLOWED_ORIGINS")
    if env_origins:
        allowed_origins_raw.extend(o.strip() for o in env_origins.split(","))
    cfg_origins = config.get("allowed_origins")
    if isinstance(cfg_origins, list):
        allowed_origins_raw.extend(cfg_origins)
    allowed_origins = [o for o in allowed_origins_raw if o]

    # Instantiate the singleton authenticator (fail-fast).
    try:
        set_authenticator(Authenticator(password=password))
    except AuthenticationError as e:
        logger.error("authentication setup failed: %s", e)
        sys.exit(2)

    # Idle timeout: CLI > env > config > default
    idle_timeout = args.session_idle_timeout
    if idle_timeout is None:
        env_idle = os.environ.get("TUNNELTERM_SESSION_IDLE_TIMEOUT")
        if env_idle is not None:
            try:
                idle_timeout = float(env_idle)
            except ValueError:
                logger.warning(
                    "ignoring invalid TUNNELTERM_SESSION_IDLE_TIMEOUT=%r", env_idle
                )
    if idle_timeout is None:
        cfg_idle = config.get("session_idle_timeout")
        if isinstance(cfg_idle, (int, float)):
            idle_timeout = float(cfg_idle)
    if idle_timeout is None:
        idle_timeout = float(DEFAULT_IDLE_TIMEOUT_SECONDS)
    if idle_timeout < 0:
        idle_timeout = float(DEFAULT_IDLE_TIMEOUT_SECONDS)

    logger.info(
        "Starting tunnelterm on %s:%d (command=%r, idle_timeout=%.0fs)",
        host, port, command, idle_timeout,
    )
    if allowed_origins:
        logger.info("Allowed origins: %s", ", ".join(allowed_origins))
    else:
        logger.info("No origin allow-list set; all origins accepted")

    from tunnelterm.main import run

    run(
        command=command,
        host=host,
        port=port,
        log_level=log_level_str.lower(),
        allowed_origins=allowed_origins,
        idle_timeout=idle_timeout,
    )


if __name__ == "__main__":
    main()
