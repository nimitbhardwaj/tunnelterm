"""PTY Manager Module for spawning and managing hermes in a pseudo-terminal."""

from __future__ import annotations

import asyncio
import logging
import os
import pty
import signal
import sys
from collections.abc import AsyncGenerator, Generator

logger = logging.getLogger(__name__)


class PtyManager:
    """Manages a PTY process running a command in a pseudo-terminal."""

    def __init__(
        self,
        command: str = "hermes",
        log_level: int = logging.DEBUG,
    ) -> None:
        """Initialize the PTY manager.

        Args:
            command: The command to run in the PTY (default: "hermes").
            log_level: Logging level for debug output (default DEBUG).

        """
        self._command = command
        self._log_level = log_level
        self._master_fd: int | None = None
        self._pid: int | None = None

    def spawn(self) -> int:
        """Spawn the command in a PTY using pty.fork().

        Returns:
            The master file descriptor for the PTY.

        Raises:
            OSError: If PTY creation fails.

        """
        import fcntl
        import struct
        import termios

        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            try:
                os.setsid()
            except PermissionError:
                pass
            os.execvp(self._command, [self._command])
            sys.exit(1)

        try:
            winsize = struct.pack("HHHH", 24, 80, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

        return self._master_fd

    def read_from_pty(self) -> Generator[bytes, None, None]:
        """Yield output bytes from the PTY.

        Yields:
            Raw bytes from the PTY master file descriptor.

        """
        master_fd = self._master_fd
        while master_fd is not None:
            try:
                data = os.read(master_fd, 4096)
                if data:
                    logger.log(self._log_level, f"Read from PTY: {len(data)} bytes")
                    yield data
                else:
                    logger.log(self._log_level, "PTY returned empty data, end of output")
                    break
            except OSError as e:
                logger.log(self._log_level, f"OSError reading from PTY: {e}")
                break

    async def write_to_pty(self, data: bytes) -> None:
        """Write input bytes to the PTY stdin.

        Args:
            data: Raw bytes to write to the PTY.

        """
        if self._master_fd is None:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, os.write, self._master_fd, data)

    async def read_from_pty_async(self) -> AsyncGenerator[bytes, None]:
        """Yield output bytes from the PTY asynchronously.

        Yields:
            Raw bytes from the PTY master file descriptor.

        """
        loop = asyncio.get_running_loop()
        while self._master_fd is not None:
            try:
                data = await loop.run_in_executor(None, os.read, self._master_fd, 4096)
                if data:
                    yield data
                else:
                    break
            except OSError:
                break

    def close(self) -> None:
        """Clean up PTY resources and kill the spawned process."""
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._pid = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY window.

        Args:
            cols: Number of columns.
            rows: Number of rows.

        """
        import fcntl
        import struct
        import termios

        if self._master_fd is None:
            return

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass


def spawn_command(command: str = "hermes", log_level: int = logging.DEBUG) -> PtyManager:
    """Create and spawn a command in a new PTY.

    Args:
        command: The command to run in the PTY (default: "hermes").
        log_level: Logging level for debug output (default DEBUG).

    Returns:
        A PtyManager instance with the spawned PTY.

    """
    manager = PtyManager(command=command, log_level=log_level)
    manager.spawn()
    return manager
