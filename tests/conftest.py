"""Shared pytest fixtures."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Generator

import pytest


def _free_port() -> int:
    """Return a free local TCP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, timeout: float = 5.0) -> bool:
    """Block until ``port`` accepts connections, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture
def server_process() -> Generator[dict, None, None]:
    """Start a tunnelterm server subprocess on a free port and yield its info."""
    port = _free_port()
    env = os.environ.copy()
    env["TUNNELTERM_PASSWORD"] = "testpass"
    env["LOG_LEVEL"] = "WARNING"
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "tunnelterm",
            "--command",
            "bash --norc --noprofile",
            "--port",
            str(port),
            "--host",
            "127.0.0.1",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_port(port, timeout=8.0):
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            proc.kill()
            pytest.fail(f"server did not start on port {port}; stderr:\n{stderr}")
        yield {"port": port, "password": "testpass", "url": f"http://127.0.0.1:{port}"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
