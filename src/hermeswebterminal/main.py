"""FastAPI server for Hermes Web Terminal."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from hermeswebterminal.auth import Authenticator
from hermeswebterminal.pty_manager import PtyManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_COMMAND = "hermes"

app = FastAPI(title="Hermes Web Terminal")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the main terminal page."""
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=index_path.read_bytes())


@app.websocket("/auth")
async def auth_ws(ws: WebSocket) -> None:
    """Handle password authentication via WebSocket."""
    await ws.accept()
    client_ip = ws.client.host if ws.client else "unknown"
    logger.debug(f"Auth request from {client_ip}")

    try:
        data = await ws.receive_json()
        password = data.get("password", "")
    except json.JSONDecodeError:
        await ws.send_json({"error": "Invalid JSON"})
        await ws.close()
        return

    auth = Authenticator()
    if auth.verify(password):
        logger.info(f"Auth success for {client_ip}")
        await ws.send_json({"token": auth.generate_token()})
    else:
        logger.warning(f"Auth failure for {client_ip}")
        await ws.send_json({"error": "Invalid password"})

    await ws.close()


@app.websocket("/ws")
async def ws_handler(
    ws: WebSocket,
    auth_token: str = Query(""),
) -> None:
    """Bridge WebSocket data to/from PTY."""
    await ws.accept()
    client_ip = ws.client.host if ws.client else "unknown"

    if not auth_token:
        logger.warning(f"Unauthorized: missing token from {client_ip}")
        await ws.close(code=1008)
        return

    auth = Authenticator()
    if not auth.check_auth(auth_token):
        logger.warning(f"Unauthorized: invalid token from {client_ip}")
        await ws.close(code=1008)
        return

    logger.info(f"Authenticated connection from {client_ip}")

    command = ws.app.state.command if hasattr(ws.app.state, "command") else DEFAULT_COMMAND
    pty = PtyManager(command=command)

    try:
        pty.spawn()
    except Exception as e:
        logger.error(f"PTY spawn failed: {e}")
        await ws.close(code=1011)
        return

    async def pty_to_ws() -> None:
        try:
            async for data in pty.read_from_pty_async():
                await ws.send_bytes(data)
                await asyncio.sleep(0)
        except Exception as e:
            logger.debug(f"PTY->WS error: {e}")

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                    if data.get("type") == "resize":
                        pty.resize(data["cols"], data["rows"])
                        continue
                except json.JSONDecodeError:
                    pass
                await pty.write_to_pty(msg.encode())
        except Exception:
            pass

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    except asyncio.CancelledError:
        pass
    finally:
        pty.close()
        try:
            await ws.send_json({"type": "process_exit"})
        except Exception:
            pass
        logger.info(f"Connection closed for {client_ip}")


def run(
    command: str | None = None,
    host: str | None = None,
    port: int | None = None,
    log_level: str | None = None,
) -> None:
    """Run the server using uvicorn. Args from CLI/env."""
    import os

    import uvicorn

    cmd = command or os.environ.get("HERMES_COMMAND", "hermes")
    h = host or os.environ.get("HERMES_HOST", "127.0.0.1")
    p = port or int(os.environ.get("HERMES_PORT", "4200"))
    ll = log_level or os.environ.get("LOG_LEVEL", "info")

    app.state.command = cmd
    uvicorn.run(app, host=h, port=p, log_level=ll, access_log=False)
