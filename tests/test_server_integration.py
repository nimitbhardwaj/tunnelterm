"""End-to-end tests against a real running server subprocess.

Covers:
- /auth round-trip
- /ws subprotocol-token handshake + echo
- /auth rate-limit lockout
- the original Ctrl+C regression: server must shut down within a few seconds
  even with an active idle client connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_server(port: int, command: str = "bash --norc --noprofile") -> subprocess.Popen:
    env = os.environ.copy()
    env["TUNNELTERM_PASSWORD"] = "testpass"
    env["LOG_LEVEL"] = "WARNING"
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "tunnelterm",
            "--command", command,
            "--port", str(port),
            "--host", "127.0.0.1",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if not _wait_port(port, timeout=8.0):
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"server did not start; stderr:\n{stderr}")
    return proc


async def _do_auth(port: int, password: str) -> dict:
    """Send password to /auth and return the JSON response."""
    from websockets.legacy.client import connect

    async with connect(f"ws://127.0.0.1:{port}/auth") as ws:
        await ws.send(json.dumps({"password": password}))
        resp = await ws.recv()
    return json.loads(resp)


async def test_auth_rejects_wrong_password() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        resp = await _do_auth(port, "WRONG")
        assert resp.get("error") == "invalid_password"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_accepts_correct_password() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        resp = await _do_auth(port, "testpass")
        assert "token" in resp
        assert len(resp["token"]) >= 20
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ws_subprotocol_handshake_and_echo() -> None:
    from websockets.legacy.client import connect

    port = _free_port()
    proc = _start_server(port)
    try:
        token = (await _do_auth(port, "testpass"))["token"]
        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            subprotocols=[f"tunnelterm.v1.token", token],  # type: ignore[arg-type]
        ) as ws:
            # Give bash a moment to print the prompt.
            await asyncio.sleep(0.3)
            await ws.send("echo PTY_OK\n")
            # Read until we see PTY_OK or timeout.
            deadline = asyncio.get_event_loop().time() + 3.0
            seen = b""
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    seen += msg
                else:
                    seen += msg.encode()
                if b"PTY_OK" in seen:
                    break
            assert b"PTY_OK" in seen, f"never saw PTY_OK; buffer = {seen[-200:]!r}"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ws_rejects_missing_token() -> None:
    from websockets.legacy.client import connect

    port = _free_port()
    proc = _start_server(port)
    try:
        # No subprotocol -> handshake should be rejected (1008).
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws") as _ws:
                pass
        except Exception:
            return  # expected
        pytest.fail("ws connection without token was unexpectedly accepted")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_rate_limit_after_many_failures() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        # 5 failures should trip the limiter.
        results = []
        for _ in range(7):
            results.append(await _do_auth(port, "BAD"))
        # At least one of the later attempts must be rate-limited.
        rate_limited = [r for r in results if r.get("error") == "rate_limited"]
        assert len(rate_limited) >= 1, f"expected lockout among: {results}"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ctrl_d_closes_session_cleanly() -> None:
    """Regression: when the shell exits (Ctrl+D), the server must send a
    {__tt: process_exit} frame AND close the WebSocket with a clean 1000 code,
    AND the child process must be reaped (no zombie)."""
    import os as _os

    from websockets.legacy.client import connect

    port = _free_port()
    proc = _start_server(port, command="bash --norc --noprofile")
    try:
        token = (await _do_auth(port, "testpass"))["token"]
        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            subprotocols=["tunnelterm.v1.token", token],  # type: ignore[arg-type]
        ) as ws:
            await asyncio.sleep(0.4)  # let bash print its prompt
            await ws.send("\x04")  # Ctrl+D (EOT)

            # Drain frames; expect process_exit and then a clean close.
            saw_exit = False
            close_code = None
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    # Connection closed; capture close code.
                    close_code = ws.close_code
                    break
                if isinstance(msg, str) and '"__tt":"process_exit"' in msg:
                    saw_exit = True

            # If the loop exited without an exception we may still need to
            # close the context manager; once closed, close_code is set.
            if close_code is None:
                close_code = ws.close_code

        assert saw_exit, "did not receive process_exit control frame"
        assert close_code == 1000, f"expected clean close 1000, got {close_code}"

        # Server process must still be alive (only the PTY child should exit).
        assert proc.poll() is None, "server died after Ctrl+D"

        # No bash --norc --noprofile zombies should remain under our server.
        # macOS `ps -o stat=` returns 'Z' for zombies.
        import subprocess as _sp

        result = _sp.run(  # noqa: S603
            ["ps", "-A", "-o", "ppid=,stat=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
        zombies = [
            line for line in result.stdout.splitlines()
            if line.strip().startswith(str(proc.pid))
            and "Z" in line.split(None, 2)[1]
        ]
        assert not zombies, f"zombie children remain: {zombies}"
        _ = _os  # silence unused-import
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_shutdown_under_idle_connection_is_fast() -> None:
    """The original Ctrl+C regression: SIGINT with an active client must
    complete shutdown within 3s, not hang for 10+s."""
    from websockets.legacy.client import connect

    port = _free_port()
    proc = _start_server(port)
    try:
        token = (await _do_auth(port, "testpass"))["token"]
        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            subprotocols=[f"tunnelterm.v1.token", token],  # type: ignore[arg-type]
        ) as ws:
            await asyncio.sleep(0.5)  # let server fully set up the bridge

            start = time.monotonic()
            proc.send_signal(signal.SIGINT)

            # Drain any remaining messages quickly so ws doesn't hold us up.
            async def _drain() -> None:
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=0.2)
                except (asyncio.TimeoutError, Exception):
                    return

            drain_task = asyncio.create_task(_drain())
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                pytest.fail("server did not shut down within 5s under SIGINT")
            elapsed = time.monotonic() - start
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass
            # Should be well under 3s.
            assert elapsed < 3.0, f"shutdown took {elapsed:.2f}s"
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
