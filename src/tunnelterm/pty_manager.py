"""PTY manager: spawn a command in a pseudo-terminal and bridge bytes."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import shlex
import signal
import struct
import sys
import termios

logger = logging.getLogger(__name__)


class PtySpawnError(Exception):
    """Raised when spawning the child command fails (e.g. exec failure)."""


class PtyManager:
    """Owns a single PTY and the child process attached to it."""

    def __init__(self, command: str) -> None:
        """Initialize.

        Args:
            command: Shell-style command string. ``shlex.split`` is applied.

        """
        self._command = command
        self._argv = shlex.split(command)
        if not self._argv:
            msg = f"empty command: {command!r}"
            raise PtySpawnError(msg)
        self._master_fd: int | None = None
        self._pid: int | None = None

    # ---------- properties ----------

    @property
    def master_fd(self) -> int | None:
        """The master file descriptor, or None if not spawned / closed."""
        return self._master_fd

    @property
    def pid(self) -> int | None:
        """Child PID, or None if not spawned / reaped."""
        return self._pid

    @property
    def command(self) -> str:
        """The original command string."""
        return self._command

    # ---------- spawn ----------

    def spawn(self, rows: int = 24, cols: int = 80) -> int:
        """Fork a PTY and exec the command.

        Uses an "exec-error" pipe so the parent learns synchronously if the
        child's ``execvp`` fails, surfacing a useful :class:`PtySpawnError`.
        """
        # Pipe (read in parent, write in child) reserved for exec failure signal.
        err_r, err_w = os.pipe()
        # CLOEXEC on the write-end so a successful exec auto-closes it (EOF in parent).
        flags = fcntl.fcntl(err_w, fcntl.F_GETFD)
        fcntl.fcntl(err_w, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)

        pid, master_fd = pty.fork()
        if pid == 0:
            # Child branch.
            os.close(err_r)
            try:
                os.setsid()
            except OSError:
                pass
            try:
                os.execvp(self._argv[0], self._argv)
            except OSError as e:
                # Send "errno: message" to the parent then exit.
                try:
                    os.write(err_w, f"{e.errno}:{e.strerror}".encode())
                except OSError:
                    pass
                os._exit(127)
            # Unreachable; satisfy type checker.
            sys.exit(1)

        # Parent branch.
        os.close(err_w)
        self._pid = pid
        self._master_fd = master_fd

        # Block-read the exec-error pipe. The child either:
        #   * succeeds in execvp() -> CLOEXEC closes err_w in the child ->
        #     parent's read returns EOF (b"")
        #   * fails -> child writes "errno:message" then _exit(127)
        # Use select with a short timeout so we don't block forever if the
        # child is unexpectedly stuck before either path.
        import select

        err_data = b""
        deadline = 2.0  # seconds total to wait for exec result
        try:
            while True:
                ready, _, _ = select.select([err_r], [], [], deadline)
                if not ready:
                    break
                try:
                    chunk = os.read(err_r, 256)
                except OSError:
                    break
                if not chunk:
                    break
                err_data += chunk
        finally:
            try:
                os.close(err_r)
            except OSError:
                pass

        if err_data:
            # Reap zombie.
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
            self._pid = None
            try:
                os.close(master_fd)
            except OSError:
                pass
            self._master_fd = None
            try:
                errno_part, _, msg_part = err_data.decode().partition(":")
                detail = msg_part or "exec failed"
                errno_int = int(errno_part) if errno_part.isdigit() else 0
            except (ValueError, UnicodeDecodeError):
                detail = "exec failed"
                errno_int = 0
            raise PtySpawnError(
                f"failed to exec {self._argv[0]!r}: {detail} (errno {errno_int})"
            )

        # Set initial window size.
        self.resize(cols=cols, rows=rows)
        logger.debug("Spawned pid=%d cmd=%r master_fd=%d", pid, self._command, master_fd)
        return master_fd

    # ---------- I/O ----------

    async def write_to_pty(self, data: bytes) -> None:
        """Write bytes to the PTY master fd."""
        if self._master_fd is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, os.write, self._master_fd, data)
        except (OSError, ValueError) as e:
            logger.debug("write_to_pty error: %s", e)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY window."""
        if self._master_fd is None:
            return
        if cols <= 0 or rows <= 0:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError as e:
            logger.debug("resize error: %s", e)

    # ---------- shutdown ----------

    def close(self) -> None:
        """Kill the child and close the master fd. Idempotent."""
        pid = self._pid
        self._pid = None
        if pid is not None:
            self._kill_process(pid)
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass

        master_copy = self._master_fd
        self._master_fd = None
        if master_copy is not None:
            try:
                os.close(master_copy)
            except OSError:
                pass

    @staticmethod
    def _kill_process(pid: int) -> None:
        """Send SIGKILL to the process group (or process)."""
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
