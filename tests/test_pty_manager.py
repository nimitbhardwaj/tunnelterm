"""Tests for PtyManager: spawn, read, resize, close, error reporting."""

from __future__ import annotations

import os
import time

import pytest

from tunnelterm.pty_manager import PtyManager, PtySpawnError


def test_spawn_simple_echo() -> None:
    """Spawn `echo hello`; expect "hello" on the master fd."""
    pty = PtyManager(command="echo hello")
    pty.spawn()
    try:
        assert pty.master_fd is not None
        # Read with a short timeout.
        os.set_blocking(pty.master_fd, False)
        deadline = time.monotonic() + 2.0
        buf = b""
        while time.monotonic() < deadline and b"hello" not in buf:
            try:
                chunk = os.read(pty.master_fd, 4096)
                if not chunk:
                    break
                buf += chunk
            except BlockingIOError:
                time.sleep(0.05)
        assert b"hello" in buf, f"expected 'hello' in output, got {buf!r}"
    finally:
        pty.close()


def test_spawn_unknown_command_raises() -> None:
    """A non-existent command surfaces as PtySpawnError, not silent."""
    pty = PtyManager(command="this_definitely_does_not_exist_xyz_12345")
    with pytest.raises(PtySpawnError):
        pty.spawn()
    assert pty.master_fd is None
    assert pty.pid is None


def test_shlex_split() -> None:
    """Multi-word --command works."""
    pty = PtyManager(command='sh -c "echo argv-ok"')
    pty.spawn()
    try:
        os.set_blocking(pty.master_fd, False)  # type: ignore[arg-type]
        deadline = time.monotonic() + 2.0
        buf = b""
        while time.monotonic() < deadline and b"argv-ok" not in buf:
            try:
                chunk = os.read(pty.master_fd, 4096)  # type: ignore[arg-type]
                if not chunk:
                    break
                buf += chunk
            except BlockingIOError:
                time.sleep(0.05)
        assert b"argv-ok" in buf
    finally:
        pty.close()


def test_empty_command_raises() -> None:
    """Empty command string is rejected at construction time."""
    with pytest.raises(PtySpawnError):
        PtyManager(command="   ")


def test_close_is_idempotent() -> None:
    """Calling close() twice doesn't blow up."""
    pty = PtyManager(command="echo bye")
    pty.spawn()
    pty.close()
    pty.close()
    assert pty.master_fd is None


def test_resize_after_spawn() -> None:
    """resize() returns silently when fd is open."""
    pty = PtyManager(command="sleep 0.5")
    pty.spawn()
    try:
        pty.resize(cols=120, rows=40)
    finally:
        pty.close()
