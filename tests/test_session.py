"""Unit tests for PtySession and SessionRegistry."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from tunnelterm.session import PtySession, SessionRegistry


@pytest.mark.asyncio
async def test_session_captures_output_to_replay_buffer() -> None:
    """Bytes from the PTY accumulate in the replay buffer."""
    sess = PtySession(token="t1", command="echo hello-replay")
    loop = asyncio.get_running_loop()
    sess.start(loop)
    try:
        # Wait until 'hello-replay' appears in the buffer.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if b"hello-replay" in sess.replay_buffer():
                break
            await asyncio.sleep(0.05)
        assert b"hello-replay" in sess.replay_buffer()
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_replay_buffer_is_capped() -> None:
    """The replay buffer doesn't grow beyond its configured cap."""
    sess = PtySession(
        token="t2",
        command='bash -c "for i in $(seq 1 5000); do echo line-$i; done"',
        replay_cap_bytes=4096,
    )
    loop = asyncio.get_running_loop()
    sess.start(loop)
    try:
        # Wait for the shell to finish producing output.
        deadline = time.monotonic() + 5.0
        last_len = -1
        stable = 0
        while time.monotonic() < deadline:
            cur = len(sess.replay_buffer())
            if cur == last_len:
                stable += 1
                if stable >= 4:
                    break
            else:
                stable = 0
                last_len = cur
            await asyncio.sleep(0.1)
        assert len(sess.replay_buffer()) <= 4096
        # And the *latest* lines should be in the buffer, not the earliest.
        buf = sess.replay_buffer()
        assert b"line-5000" in buf or b"line-499" in buf, buf[-200:]
        assert b"line-1\n" not in buf
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_session_attach_detach_tracks_state() -> None:
    """attach/detach toggle the `attached` flag and last_detached_at."""
    sess = PtySession(token="t3", command="bash --norc --noprofile")
    loop = asyncio.get_running_loop()
    sess.start(loop)
    try:
        assert sess.attached is False
        assert sess.last_detached_at is None

        def cb(_data: bytes) -> None:
            pass

        sess.attach(cb)
        assert sess.attached is True
        assert sess.last_detached_at is None

        sess.detach(cb)
        assert sess.attached is False
        assert sess.last_detached_at is not None
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_registry_get_or_create_returns_same_session() -> None:
    """Calling get_or_create twice with the same token returns the same object."""
    registry = SessionRegistry(command="bash --norc --noprofile", idle_timeout=60)
    loop = asyncio.get_running_loop()
    try:
        s1 = registry.get_or_create("tok", loop=loop)
        s2 = registry.get_or_create("tok", loop=loop)
        assert s1 is s2
        assert registry.session_count() == 1
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_registry_idle_reaper_kills_detached_session() -> None:
    """A detached session whose idle timeout elapsed is reaped."""
    registry = SessionRegistry(
        command="bash --norc --noprofile",
        idle_timeout=0.5,
        reaper_interval=0.1,
    )
    loop = asyncio.get_running_loop()
    registry.start(loop)
    try:
        sess = registry.get_or_create("idle-tok", loop=loop)
        # Simulate attach then detach (same callable instance so detach finds it).
        cb: Callable[[bytes], None] = lambda _d: None  # noqa: E731
        sess.attach(cb)
        sess.detach(cb)
        # Wait long enough for the reaper.
        await asyncio.sleep(1.0)
        assert registry.get("idle-tok") is None
        assert registry.session_count() == 0
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_registry_does_not_reap_attached_session() -> None:
    """An attached session is NOT reaped even past its idle timeout."""
    registry = SessionRegistry(
        command="bash --norc --noprofile",
        idle_timeout=0.2,
        reaper_interval=0.1,
    )
    loop = asyncio.get_running_loop()
    registry.start(loop)
    try:
        sess = registry.get_or_create("live-tok", loop=loop)
        # Permanently attached.
        sess.attach(lambda _d: None)
        await asyncio.sleep(0.6)
        assert registry.get("live-tok") is sess
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_registry_get_or_create_respawns_when_pty_died() -> None:
    """If a stored session's PTY died, get_or_create discards and respawns."""
    registry = SessionRegistry(command="echo gone", idle_timeout=60)
    loop = asyncio.get_running_loop()
    try:
        s1 = registry.get_or_create("respawn-tok", loop=loop)
        # `echo gone` exits immediately. Give the event loop a beat to notice.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and s1.is_alive():
            await asyncio.sleep(0.05)
        assert not s1.is_alive(), "PTY should have EOFed after 'echo gone'"

        # Now get_or_create with the same token must give us a NEW session.
        registry.command = "bash --norc --noprofile"
        s2 = registry.get_or_create("respawn-tok", loop=loop)
        assert s2 is not s1
        assert s2.is_alive()
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_registry_discard_kills_session() -> None:
    """discard() removes and closes a session (for logout flow)."""
    registry = SessionRegistry(command="bash --norc --noprofile", idle_timeout=60)
    loop = asyncio.get_running_loop()
    try:
        sess = registry.get_or_create("discard-tok", loop=loop)
        registry.discard("discard-tok")
        assert registry.get("discard-tok") is None
        assert not sess.is_alive()
    finally:
        await registry.shutdown()
