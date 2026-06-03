"""End-to-end tests against a real running server subprocess.

Covers the cookie-based auth surface introduced in v0.1.4:

- POST /api/auth -> sets Set-Cookie: tt_session
- POST /api/verify reads cookie -> {ok}
- POST /api/logout clears cookie and kills sticky session
- /ws reads cookie from handshake
- /ws rejects connections without a cookie or with a stale cookie
- Sticky-session: same cookie across two /ws connects reuses the PTY
- Ctrl+D / Ctrl+C regressions still pass
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

import httpx
import pyotp
import pytest
from websockets.legacy.client import connect

pytestmark = pytest.mark.asyncio

COOKIE_NAME = "tt_session"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # standard "Hello!" Base32 test secret


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


def _start_server(
    port: int,
    command: str = "bash --norc --noprofile",
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["TUNNELTERM_PASSWORD"] = "testpass"
    env["LOG_LEVEL"] = "WARNING"
    if extra_env:
        env.update(extra_env)
    args = [
        sys.executable, "-m", "tunnelterm",
        "--command", command,
        "--port", str(port),
        "--host", "127.0.0.1",
    ]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.Popen(  # noqa: S603
        args,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if not _wait_port(port, timeout=8.0):
        proc.kill()
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(f"server did not start; stderr:\n{stderr}")
    return proc


async def _login(
    port: int,
    password: str,
    totp: str | None = None,
) -> tuple[int, dict, str | None]:
    """POST /api/auth; return (status, json, cookie-value-or-None)."""
    body: dict = {"password": password}
    if totp is not None:
        body["totp"] = totp
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        r = await client.post("/api/auth", json=body)
    token = r.cookies.get(COOKIE_NAME)
    try:
        body_json = r.json()
    except ValueError:
        body_json = {}
    return r.status_code, body_json, token


async def test_auth_rejects_wrong_password() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        status, body, token = await _login(port, "WRONG")
        assert status == 401
        assert body.get("error") == "invalid_password"
        assert token is None
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_sets_cookie_on_success() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        status, body, token = await _login(port, "testpass")
        assert status == 200
        assert body == {"ok": True}
        assert token is not None
        assert len(token) >= 20
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_cookie_attributes_are_secure() -> None:
    """Cookie must be HttpOnly + SameSite=Strict at minimum."""
    port = _free_port()
    proc = _start_server(port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post("/api/auth", json={"password": "testpass"})
        set_cookie = r.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie or "samesite=strict" in set_cookie.lower()
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_cookie_is_persistent() -> None:
    """Cookie always carries Max-Age matching the token TTL (no per-request opt-in)."""
    port = _free_port()
    proc = _start_server(port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post("/api/auth", json={"password": "testpass"})
        set_cookie = r.headers.get("set-cookie", "")
        # Default token TTL is 24h = 86400s; expect a Max-Age in that ballpark.
        lower = set_cookie.lower()
        assert "max-age=" in lower
        # Parse the Max-Age value and sanity-check it.
        for part in set_cookie.split(";"):
            part = part.strip()
            if part.lower().startswith("max-age="):
                value = int(part.split("=", 1)[1])
                assert 3600 <= value <= 7 * 24 * 3600
                break
        else:
            pytest.fail(f"no Max-Age found in: {set_cookie!r}")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ws_with_cookie_echoes() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        _, _, token = await _login(port, "testpass")
        assert token
        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            extra_headers={"Cookie": f"{COOKIE_NAME}={token}"},
        ) as ws:
            await asyncio.sleep(0.3)
            await ws.send("echo PTY_OK\n")
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
            assert b"PTY_OK" in seen, f"never saw PTY_OK; tail={seen[-200:]!r}"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ws_rejects_no_cookie() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws") as _ws:
                pass
        except Exception:
            return  # expected: handshake rejected
        pytest.fail("ws connection without cookie was unexpectedly accepted")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ws_rejects_bogus_cookie() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        try:
            async with connect(
                f"ws://127.0.0.1:{port}/ws",
                extra_headers={"Cookie": f"{COOKIE_NAME}=not-a-real-token"},
            ) as _ws:
                pass
        except Exception:
            return  # expected
        pytest.fail("ws connection with bogus cookie was unexpectedly accepted")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_rate_limit_after_many_failures() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        statuses: list[int] = []
        for _ in range(7):
            status, _, _ = await _login(port, "BAD")
            statuses.append(status)
        # At least one of the later attempts must be rate-limited (429).
        assert 429 in statuses, f"expected lockout among: {statuses}"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_verify_endpoint_uses_cookie() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        # Login to populate cookie jar.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r_auth = await client.post("/api/auth", json={"password": "testpass"})
            assert r_auth.status_code == 200
            # Same client carries the cookie automatically.
            r_ok = await client.post("/api/verify")
            assert r_ok.status_code == 200
            assert r_ok.json() == {"ok": True}

        # Fresh client with no cookie -> ok=false.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r_no = await client.post("/api/verify")
            assert r_no.status_code == 200
            assert r_no.json() == {"ok": False}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_verify_rate_limits_excessive_hits() -> None:
    """The /api/verify endpoint enforces a per-IP per-minute cap."""
    port = _free_port()
    proc = _start_server(port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            # Default cap is 300/min; fire 320 in a tight loop.
            statuses = []
            for _ in range(320):
                r = await client.post("/api/verify")
                statuses.append(r.status_code)
        assert 429 in statuses, f"verify rate-limit never tripped: {statuses[-10:]}"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_logout_clears_cookie_and_session() -> None:
    port = _free_port()
    proc = _start_server(port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r_auth = await client.post("/api/auth", json={"password": "testpass"})
            assert r_auth.status_code == 200
            token = client.cookies.get(COOKIE_NAME)
            assert token

            # Establish a sticky session so logout has something to discard.
            async with connect(
                f"ws://127.0.0.1:{port}/ws",
                extra_headers={"Cookie": f"{COOKIE_NAME}={token}"},
            ) as ws:
                await asyncio.sleep(0.3)
                await ws.send("MARKER=must-not-survive-logout\n")
                await asyncio.sleep(0.3)

            r_out = await client.post("/api/logout")
            assert r_out.status_code == 200
            assert r_out.json() == {"ok": True}

            # Cookie value should now be empty (Set-Cookie ... ="" Max-Age=0).
            set_cookie = r_out.headers.get("set-cookie", "")
            assert "tt_session=" in set_cookie
            assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie.lower()

            # Token must no longer be valid (logout already wiped the jar's cookie).
            client.cookies.set(COOKIE_NAME, token)
            r_verify = await client.post("/api/verify")
            assert r_verify.json() == {"ok": False}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_refresh_preserves_shell_state() -> None:
    """The same cookie across two /ws connects must hit the same PTY."""
    port = _free_port()
    proc = _start_server(port)
    try:
        _, _, token = await _login(port, "testpass")
        assert token
        cookie_header = f"{COOKIE_NAME}={token}"

        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            extra_headers={"Cookie": cookie_header},
        ) as ws:
            await asyncio.sleep(0.4)
            await ws.send("MARKER=preserved-across-refresh\n")
            await asyncio.sleep(0.4)
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                pass

        await asyncio.sleep(0.3)

        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            extra_headers={"Cookie": cookie_header},
        ) as ws:
            await asyncio.sleep(0.3)
            replay_chunks: list[bytes] = []
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.4)
                    if isinstance(msg, bytes):
                        replay_chunks.append(msg)
            except asyncio.TimeoutError:
                pass
            replay = b"".join(replay_chunks)
            assert b"MARKER=preserved-across-refresh" in replay, (
                f"scrollback replay missing marker; tail={replay[-200:]!r}"
            )

            await ws.send("echo TEST-$MARKER\n")
            seen = b""
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.4)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    seen += msg
                if b"TEST-preserved-across-refresh" in seen:
                    break
            assert b"TEST-preserved-across-refresh" in seen, (
                f"shell variable did not survive disconnect; tail={seen[-200:]!r}"
            )
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_ctrl_d_closes_session_cleanly() -> None:
    """When the shell exits, server sends process_exit + clean WS close + no zombie."""
    port = _free_port()
    proc = _start_server(port, command="bash --norc --noprofile")
    try:
        _, _, token = await _login(port, "testpass")
        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            extra_headers={"Cookie": f"{COOKIE_NAME}={token}"},
        ) as ws:
            await asyncio.sleep(0.4)
            await ws.send("\x04")  # Ctrl+D

            saw_exit = False
            close_code = None
            deadline = asyncio.get_event_loop().time() + 5.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    close_code = ws.close_code
                    break
                if isinstance(msg, str) and '"__tt":"process_exit"' in msg:
                    saw_exit = True

            if close_code is None:
                close_code = ws.close_code

        assert saw_exit, "did not receive process_exit"
        assert close_code == 1000, f"expected clean close 1000, got {close_code}"
        assert proc.poll() is None, "server died after Ctrl+D"

        result = subprocess.run(  # noqa: S603
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
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_shutdown_under_idle_connection_is_fast() -> None:
    """SIGINT with an active client completes within 3s."""
    port = _free_port()
    proc = _start_server(port)
    try:
        _, _, token = await _login(port, "testpass")
        async with connect(
            f"ws://127.0.0.1:{port}/ws",
            extra_headers={"Cookie": f"{COOKIE_NAME}={token}"},
        ) as ws:
            await asyncio.sleep(0.5)

            start = time.monotonic()
            proc.send_signal(signal.SIGINT)

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
            assert elapsed < 3.0, f"shutdown took {elapsed:.2f}s"
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


async def test_refuses_nonloopback_without_origin_allowlist() -> None:
    """A non-loopback bind without --allowed-origin must refuse to start."""
    port = _free_port()
    env = os.environ.copy()
    env["TUNNELTERM_PASSWORD"] = "testpass"
    env["LOG_LEVEL"] = "WARNING"
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "tunnelterm",
            "--command", "bash --norc --noprofile",
            "--port", str(port),
            "--host", "0.0.0.0",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    rc = proc.wait(timeout=5)
    stderr = (proc.stderr.read() if proc.stderr else b"").decode()
    assert rc == 2, f"expected exit code 2, got {rc}; stderr={stderr!r}"
    assert "non-loopback" in stderr.lower() or "allow" in stderr.lower(), stderr


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) second-factor tests
# ---------------------------------------------------------------------------


async def test_totp_required_rejects_password_only() -> None:
    """With TOTP required, password alone is rejected."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--require-totp"],
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        status, body, token = await _login(port, "testpass")
        assert status == 401
        assert body.get("error") == "invalid_credentials"
        assert token is None
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_totp_required_rejects_wrong_code() -> None:
    """Correct password + wrong TOTP code is rejected."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--require-totp"],
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        status, body, token = await _login(port, "testpass", totp="000000")
        assert status == 401
        assert body.get("error") == "invalid_credentials"
        assert token is None
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_totp_required_accepts_valid_code() -> None:
    """Correct password + correct TOTP code mints a session cookie."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--require-totp"],
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        code = pyotp.TOTP(TOTP_SECRET).now()
        status, body, token = await _login(port, "testpass", totp=code)
        assert status == 200, body
        assert body == {"ok": True}
        assert token is not None
        assert len(token) >= 20

        # The session cookie must actually work.
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            client.cookies.set(COOKIE_NAME, token)
            r = await client.post("/api/verify")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_totp_required_rejects_non_digit_code() -> None:
    """Non-digit TOTP is rejected, never crashes."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--require-totp"],
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        status, body, token = await _login(port, "testpass", totp="abcdef")
        assert status == 401
        assert body.get("error") == "invalid_credentials"
        assert token is None
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_totp_not_required_when_flag_absent() -> None:
    """Secret in env + no --require-totp => password alone still works."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        status, body, token = await _login(port, "testpass")
        assert status == 200
        assert token is not None
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_totp_refuses_to_start_when_required_but_no_secret() -> None:
    """--require-totp without a secret must fail at startup, not silently degrade."""
    port = _free_port()
    env = os.environ.copy()
    env["TUNNELTERM_PASSWORD"] = "testpass"
    env["LOG_LEVEL"] = "WARNING"
    env.pop("TUNNELTERM_TOTP_SECRET", None)
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "tunnelterm",
            "--command", "bash --norc --noprofile",
            "--port", str(port),
            "--host", "127.0.0.1",
            "--require-totp",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    rc = proc.wait(timeout=5)
    stderr = (proc.stderr.read() if proc.stderr else b"").decode()
    assert rc == 2, f"expected exit code 2, got {rc}; stderr={stderr!r}"
    assert "totp" in stderr.lower(), stderr


async def test_totp_refuses_to_start_with_invalid_secret() -> None:
    """A malformed TOTP secret must fail at startup."""
    port = _free_port()
    env = os.environ.copy()
    env["TUNNELTERM_PASSWORD"] = "testpass"
    env["LOG_LEVEL"] = "WARNING"
    env["TUNNELTERM_TOTP_SECRET"] = "not!valid!base32!!!"
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "tunnelterm",
            "--command", "bash --norc --noprofile",
            "--port", str(port),
            "--host", "127.0.0.1",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    rc = proc.wait(timeout=5)
    stderr = (proc.stderr.read() if proc.stderr else b"").decode()
    assert rc == 2, f"expected exit code 2, got {rc}; stderr={stderr!r}"
    assert "totp" in stderr.lower(), stderr


async def test_auth_mode_endpoint_reports_require_totp() -> None:
    """GET /api/auth/mode returns require_totp=true when TOTP is enforced."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--require-totp"],
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.get("/api/auth/mode")
        assert r.status_code == 200
        assert r.json() == {"require_totp": True}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_mode_endpoint_reports_no_totp_by_default() -> None:
    """GET /api/auth/mode returns require_totp=false when TOTP is not configured."""
    port = _free_port()
    proc = _start_server(port)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.get("/api/auth/mode")
        assert r.status_code == 200
        assert r.json() == {"require_totp": False}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_mode_endpoint_secret_without_require() -> None:
    """Secret configured but --require-totp absent => require_totp=false."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.get("/api/auth/mode")
        assert r.status_code == 200
        assert r.json() == {"require_totp": False}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


# ---------------------------------------------------------------------------
# Origin-allow-list + safe-method exemption regression tests
# (see routes/auth_routes.py::_origin_check_ok).
#
# The bug: browsers do NOT send the Origin header on same-origin GETs (per
# the Fetch spec, Origin is only sent on cross-origin or state-changing
# requests). When an operator configures --allowed-origin, the same-origin
# GET to /api/auth/mode arrived with no Origin header and was 403'd by
# origin_allowed(None, allow_list) -> False. The JS authMode() probe then
# silently fell back to {require_totp: false} and the TOTP field stayed
# hidden on initial page load. The fix exempts safe methods (GET/HEAD/
# OPTIONS) from the origin check.
# ---------------------------------------------------------------------------

PROD_ORIGIN = "https://nangadaaku-openfang.duckdns.org"


async def test_auth_mode_get_works_with_allowed_origin_no_origin_header() -> None:
    """GET /api/auth/mode from a same-origin browser (no Origin header)
    must succeed when --allowed-origin is configured. Regression for the
    bug where the TOTP field stayed hidden until a failed submit."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_env={"TUNNELTERM_TOTP_SECRET": TOTP_SECRET},
        extra_args=["--require-totp", "--allowed-origin", PROD_ORIGIN],
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.get("/api/auth/mode")
        assert r.status_code == 200, r.text
        assert r.json() == {"require_totp": True}
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_auth_mode_get_works_with_bad_origin() -> None:
    """GET /api/auth/mode with a non-allow-listed Origin is also fine --
    safe methods are exempt from the check entirely."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--allowed-origin", PROD_ORIGIN],
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.get(
                "/api/auth/mode", headers={"Origin": "https://evil.example.com"}
            )
        assert r.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_post_auth_still_rejects_missing_origin_with_allowed_origin() -> None:
    """The fix must not weaken POST origin enforcement: a POST /api/auth
    with no Origin header is still 403'd when --allowed-origin is set."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--allowed-origin", PROD_ORIGIN],
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post("/api/auth", json={"password": "x"})
        assert r.status_code == 403
        assert r.json().get("error") == "origin_not_allowed"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_post_auth_still_rejects_bad_origin_with_allowed_origin() -> None:
    """POST /api/auth with a non-allow-listed Origin is still 403'd."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--allowed-origin", PROD_ORIGIN],
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post(
                "/api/auth",
                json={"password": "x"},
                headers={"Origin": "https://evil.example.com"},
            )
        assert r.status_code == 403
        assert r.json().get("error") == "origin_not_allowed"
    finally:
        proc.terminate()
        proc.wait(timeout=3)


async def test_post_auth_accepts_allow_listed_origin() -> None:
    """POST /api/auth with an allow-listed Origin reaches the password
    check (returns 401 invalid_password, not 403 origin_not_allowed)."""
    port = _free_port()
    proc = _start_server(
        port,
        extra_args=["--allowed-origin", PROD_ORIGIN],
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            r = await client.post(
                "/api/auth",
                json={"password": "wrong"},
                headers={"Origin": PROD_ORIGIN},
            )
        assert r.status_code == 401
        assert r.json().get("error") == "invalid_password"
    finally:
        proc.terminate()
        proc.wait(timeout=3)
