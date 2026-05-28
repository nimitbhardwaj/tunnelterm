"""Sticky PTY sessions keyed by auth token.

A :class:`PtySession` owns a PTY and persists across WebSocket connect /
disconnect cycles. The shell process keeps running while no client is
attached, so a page refresh (or a brief network drop) reattaches to the
same shell instead of spawning a new one.

The :class:`SessionRegistry` is the process-wide map of ``token -> PtySession``
and runs a background reaper that kills sessions whose last detach was longer
ago than ``idle_timeout`` seconds.

Lifecycle::

       acquire(token)               release()                 attach again
    --------------->  ATTACHED  ----------------->  DETACHED  -------------> ATTACHED
                                                       |
                                            idle timeout elapses
                                                       v
                                                    CLOSED
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import Callable

from tunnelterm.pty_manager import PtyManager, PtySpawnError

logger = logging.getLogger(__name__)

# Bytes of PTY output retained for replay on reconnect.
DEFAULT_REPLAY_BUFFER_BYTES = 1024 * 1024  # 1 MiB
# Default 5 hours; CLI/env can override.
DEFAULT_IDLE_TIMEOUT_SECONDS = 5 * 60 * 60
# How often the reaper checks for expired sessions.
REAPER_INTERVAL_SECONDS = 30.0


class PtySession:
    """A PTY plus state that survives the WebSocket attached to it.

    The session owns:
      * a :class:`PtyManager` (and therefore the shell process);
      * a ring buffer of recent PTY output, replayed to a reattaching client;
      * the last known terminal dimensions, re-applied on reattach;
      * an attach state used by the idle reaper.

    Concurrency: all methods are intended to be called from a single asyncio
    event loop. There is no cross-thread access.
    """

    __slots__ = (
        "_buffer",
        "_buffer_bytes",
        "_buffer_cap",
        "_cols",
        "_data_listeners",
        "_loop",
        "_pty",
        "_reader_registered",
        "_rows",
        "_token",
        "attached",
        "command",
        "created_at",
        "last_detached_at",
    )

    def __init__(
        self,
        token: str,
        command: str,
        replay_cap_bytes: int = DEFAULT_REPLAY_BUFFER_BYTES,
    ) -> None:
        """Initialize. The PTY is not spawned until :meth:`start` is called."""
        self._token = token
        self.command = command
        self._pty = PtyManager(command=command)
        # Ring buffer: deque of (bytes,) plus a running byte count to keep the
        # cap O(1) per push regardless of chunk size distribution.
        self._buffer: deque[bytes] = deque()
        self._buffer_bytes = 0
        self._buffer_cap = replay_cap_bytes
        self._cols = 80
        self._rows = 24
        self._data_listeners: list[Callable[[bytes], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_registered = False
        self.attached = False
        self.created_at = time.monotonic()
        self.last_detached_at: float | None = None

    # ---------- properties ----------

    @property
    def token(self) -> str:
        """The auth token this session is bound to."""
        return self._token

    @property
    def pty(self) -> PtyManager:
        """The underlying PtyManager."""
        return self._pty

    @property
    def dimensions(self) -> tuple[int, int]:
        """Last known (cols, rows)."""
        return self._cols, self._rows

    def replay_buffer(self) -> bytes:
        """Return a flat snapshot of the replay ring buffer."""
        return b"".join(self._buffer)

    # ---------- lifecycle ----------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Spawn the PTY and register an event-loop reader for output.

        Raises:
            PtySpawnError: if the child command cannot be exec()ed.

        """
        self._loop = loop
        self._pty.spawn(rows=self._rows, cols=self._cols)
        fd = self._pty.master_fd
        if fd is None:
            msg = "PTY spawn returned no master fd"
            raise PtySpawnError(msg)
        loop.add_reader(fd, self._on_readable)
        self._reader_registered = True
        logger.debug("PtySession started: token=%s... pid=%s", self._token[:8], self._pty.pid)

    def close(self) -> None:
        """Kill the shell and free OS resources. Idempotent."""
        loop = self._loop
        fd = self._pty.master_fd
        if loop is not None and self._reader_registered and fd is not None:
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            self._reader_registered = False
        try:
            self._pty.close()
        except Exception as e:
            logger.debug("PtySession close error: %s", e)
        logger.debug("PtySession closed: token=%s...", self._token[:8])

    def is_alive(self) -> bool:
        """Return True until :meth:`close` has been called and the fd is gone."""
        return self._pty.master_fd is not None

    # ---------- attach / detach ----------

    def attach(self, on_data: Callable[[bytes], None]) -> None:
        """Subscribe a callback to receive every byte produced by the PTY.

        The session may have at most one attached listener at a time; the
        :class:`SessionRegistry`'s single-active-session check enforces that.
        """
        self._data_listeners.append(on_data)
        self.attached = True
        self.last_detached_at = None

    def detach(self, on_data: Callable[[bytes], None]) -> None:
        """Unsubscribe a callback. Marks the session as detached for the reaper."""
        try:
            self._data_listeners.remove(on_data)
        except ValueError:
            pass
        if not self._data_listeners:
            self.attached = False
            self.last_detached_at = time.monotonic()

    # ---------- IO ----------

    async def write(self, data: bytes) -> None:
        """Forward bytes from a client into the PTY."""
        await self._pty.write_to_pty(data)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY and remember the new dimensions."""
        if cols <= 0 or rows <= 0:
            return
        self._cols = cols
        self._rows = rows
        self._pty.resize(cols=cols, rows=rows)

    # ---------- internals ----------

    def _on_readable(self) -> None:
        """Event-loop reader: pull bytes off the PTY and fan them out.

        Bytes are pushed into the ring buffer for later replay AND delivered
        to any currently-attached client(s) synchronously. If the PTY reports
        EOF (either ``read() == 0`` or an ``OSError`` such as EIO on macOS),
        the session is torn down so :meth:`is_alive` returns False.
        """
        fd = self._pty.master_fd
        if fd is None:
            return
        try:
            data = os.read(fd, 65536)
        except (OSError, ValueError):
            # PTY closed, e.g. child exited (macOS reports EIO instead of EOF).
            self._handle_eof()
            return
        if not data:
            # Linux returns 0 bytes when the child closes the slave PTY end.
            self._handle_eof()
            return

        self._push_buffer(data)
        for cb in list(self._data_listeners):
            try:
                cb(data)
            except Exception as e:
                logger.debug("data listener error: %s", e)

    def _handle_eof(self) -> None:
        """Tear down the session after the child has exited / PTY EOF.

        Notifies any attached listeners that no more data will arrive, then
        removes the loop reader and calls :meth:`PtyManager.close`. After
        this, :meth:`is_alive` returns False and the registry's bridge will
        send the user a ``process_exit`` control frame.
        """
        loop = self._loop
        fd = self._pty.master_fd
        if loop is not None and self._reader_registered and fd is not None:
            try:
                loop.remove_reader(fd)
            except (ValueError, OSError):
                pass
            self._reader_registered = False
        for cb in list(self._data_listeners):
            try:
                cb(b"")
            except Exception as e:
                logger.debug("data listener error on EOF: %s", e)
        # Reap the child and free the fd so is_alive() flips to False.
        try:
            self._pty.close()
        except Exception as e:
            logger.debug("pty.close() on EOF error: %s", e)
        logger.debug("PtySession EOF: token=%s...", self._token[:8])

    def _push_buffer(self, data: bytes) -> None:
        """Append ``data`` to the ring buffer, evicting from the head as needed."""
        self._buffer.append(data)
        self._buffer_bytes += len(data)
        while self._buffer_bytes > self._buffer_cap and self._buffer:
            old = self._buffer.popleft()
            self._buffer_bytes -= len(old)


class SessionRegistry:
    """Process-wide ``token -> PtySession`` map with an idle reaper."""

    def __init__(
        self,
        command: str,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        reaper_interval: float = REAPER_INTERVAL_SECONDS,
    ) -> None:
        """Initialize. Use :meth:`start` to launch the reaper."""
        self.command = command
        self.idle_timeout = idle_timeout
        self._sessions: dict[str, PtySession] = {}
        self._reaper_task: asyncio.Task[None] | None = None
        self._reaper_interval = reaper_interval

    # ---------- start / stop ----------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Launch the background reaper task on ``loop``."""
        if self._reaper_task is None:
            self._reaper_task = loop.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        """Kill all sessions and stop the reaper. Safe to call multiple times."""
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None
        # Close all sessions.
        for sess in list(self._sessions.values()):
            try:
                sess.close()
            except Exception as e:
                logger.debug("session close on shutdown: %s", e)
        self._sessions.clear()

    # ---------- public API ----------

    def get(self, token: str) -> PtySession | None:
        """Look up an existing session for ``token``, or None."""
        return self._sessions.get(token)

    def get_or_create(
        self,
        token: str,
        loop: asyncio.AbstractEventLoop,
    ) -> PtySession:
        """Return the existing session for ``token``, or spawn a new one.

        Raises:
            PtySpawnError: if a new PTY had to be spawned and exec failed.

        """
        existing = self._sessions.get(token)
        if existing is not None and existing.is_alive():
            return existing
        # Either no session, or its PTY died (e.g. shell exited while detached).
        if existing is not None:
            existing.close()
            self._sessions.pop(token, None)
        sess = PtySession(token=token, command=self.command)
        sess.start(loop)
        self._sessions[token] = sess
        logger.info("session created for token=%s...", token[:8])
        return sess

    def discard(self, token: str) -> None:
        """Forcibly remove and close the session for ``token`` (e.g. on logout)."""
        sess = self._sessions.pop(token, None)
        if sess is not None:
            sess.close()
            logger.info("session discarded for token=%s...", token[:8])

    def session_count(self) -> int:
        """Return the number of live sessions (for diagnostics / tests)."""
        return len(self._sessions)

    # ---------- internals ----------

    async def _reaper_loop(self) -> None:
        """Periodically purge sessions whose detach time exceeds idle_timeout."""
        try:
            while True:
                await asyncio.sleep(self._reaper_interval)
                self._reap_idle()
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("session reaper crashed: %s", e)

    def _reap_idle(self) -> None:
        """Kill any detached session past the idle timeout, or whose PTY died."""
        now = time.monotonic()
        doomed: list[str] = []
        for token, sess in self._sessions.items():
            if not sess.is_alive():
                doomed.append(token)
                continue
            if sess.attached:
                continue
            if sess.last_detached_at is None:
                # Brand-new but unattached (rare race); give it a grace period.
                continue
            if now - sess.last_detached_at >= self.idle_timeout:
                doomed.append(token)
        for token in doomed:
            sess = self._sessions.pop(token, None)
            if sess is None:
                continue
            try:
                sess.close()
            except Exception as e:
                logger.debug("reaper close error: %s", e)
            logger.info("session reaped (idle): token=%s...", token[:8])
