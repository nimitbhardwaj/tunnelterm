"""CLI entrypoint for hermes-web-terminal."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "hermes-web-terminal" / "config.toml"
ENV_PASSWORD_VAR = "HERMES_WEB_TERMINAL_PASSWORD"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4200
DEFAULT_COMMAND = "hermes"

__version__ = "0.1.0"


def _load_config(config_path: Path | None = None) -> dict:
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


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        Configured ArgumentParser instance.

    """
    parser = argparse.ArgumentParser(
        prog="hermes-web-terminal",
        description="Web-based terminal interface with PTY support.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help=f"Host to bind to (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to bind to (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--password-env",
        type=str,
        default=None,
        dest="password_env",
        help=f"Environment variable name containing the password (default: {ENV_PASSWORD_VAR})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config TOML file",
    )
    parser.add_argument(
        "--command",
        type=str,
        default=None,
        help=f"Command to run in the PTY (default: {DEFAULT_COMMAND})",
    )
    return parser


async def _async_main(
    host: str,
    port: int,
    command: str,
    password_env: str | None,
    shutdown_event: asyncio.Event,
) -> None:
    """Async main entry point.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        command: Command to run in the PTY.
        password_env: Name of the environment variable containing the password.
        shutdown_event: Event to signal shutdown initiation.

    """
    from hermeswebterminal.server import run_server

    await run_server(host=host, port=port, command=command, shutdown_event=shutdown_event)


def main() -> None:
    """Run the CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)

    config = _load_config(args.config)

    host = args.host or config.get("host", DEFAULT_HOST)
    port = args.port or config.get("port", DEFAULT_PORT)
    command = args.command or config.get("command", DEFAULT_COMMAND)

    password_env = args.password_env or ENV_PASSWORD_VAR
    if password_env in os.environ:
        logger.info(f"Using password from environment variable: {password_env}")
    elif config.get("password"):
        logger.info("Using password from config file")
    else:
        logger.warning("No password configured - authentication will fail")

    password = os.environ.get(password_env) or config.get("password") or ""
    os.environ[ENV_PASSWORD_VAR] = password

    shutdown_event = asyncio.Event()

    def handle_signal(sig: int, frame: object) -> None:
        logger.info(f"Received signal {sig}, initiating graceful shutdown...")
        logger.info(
            "Shutdown sequence: closing connections, killing PTY processes, exiting cleanly"
        )
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(
            _async_main(
                host=host,
                port=port,
                command=command,
                password_env=password_env,
                shutdown_event=shutdown_event,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
