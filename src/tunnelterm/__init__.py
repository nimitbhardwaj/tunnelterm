"""tunnelterm: web-based PTY terminal."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tunnelterm")
except PackageNotFoundError:  # editable install without metadata yet
    __version__ = "0.0.0"

__all__ = ["__version__"]
