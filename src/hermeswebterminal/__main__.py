"""CLI entrypoint for hermes-web-terminal."""

from __future__ import annotations

import argparse
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
    """Load configuration from TOML file."""
    path = config_path or CONFIG_PATH
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def main() -> None:
    """Run the CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="hermes-web-terminal",
        description="Web-based terminal interface with PTY support.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--host", type=str, default=None, help=f"Host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"Port (default: {DEFAULT_PORT})")
    parser.add_argument("--password-env", type=str, default=None, dest="password_env", help="Env var for password")
    parser.add_argument("--config", type=Path, default=None, help="Config TOML path")
    parser.add_argument("--command", type=str, default=None, help=f"Command (default: {DEFAULT_COMMAND})")
    parser.add_argument("--log-level", type=str, default=None, dest="log_level", help="Log level (DEBUG, INFO, WARNING, ERROR)")
    args = parser.parse_args()

    log_level_str = args.log_level or os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logger = logging.getLogger(__name__)

    config = _load_config(args.config)
    host = args.host or config.get("host", DEFAULT_HOST)
    port = args.port or config.get("port", DEFAULT_PORT)
    command = args.command or config.get("command", DEFAULT_COMMAND)

    password_env = args.password_env or ENV_PASSWORD_VAR
    if password_env in os.environ:
        logger.info(f"Using password from env var: {password_env}")
    elif config.get("password"):
        logger.info("Using password from config file")
    else:
        logger.warning("No password configured - authentication will fail")

    password = os.environ.get(password_env) or config.get("password") or ""
    os.environ[ENV_PASSWORD_VAR] = password

    def handle_signal(sig: int, frame: object) -> None:
        logger.info(f"Received signal {sig}, shutting down...")
        import sys
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    from hermeswebterminal.main import run
    run(command=command, host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
