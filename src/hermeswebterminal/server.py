"""Server Module for HTTP and WebSocket serving."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp
import aiohttp.web

from hermeswebterminal.auth import Authenticator
from hermeswebterminal.pty_manager import PtyManager

logger = logging.getLogger(__name__)

DEBUG_MESSAGES = False

STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4200
DEFAULT_COMMAND = "hermes"


async def index_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Serve the index.html file."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        content = index_path.read_bytes()
        return aiohttp.web.Response(body=content, content_type="text/html")
    return aiohttp.web.Response(status=404, text="Not Found")


async def auth_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Handle authentication via WebSocket."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    client_ip = request.remote if request.remote else "unknown"
    logger.debug(f"Auth request from {client_ip}")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    password = data.get("password", "")
                except json.JSONDecodeError:
                    await ws.send_json({"error": "Invalid JSON"})
                    continue

                authenticator = Authenticator()
                if authenticator.verify(password):
                    token = authenticator.generate_token()
                    logger.info(f"Auth success for {client_ip}")
                    await ws.send_json({"token": token})
                else:
                    logger.warning(f"Auth failure for {client_ip}")
                    await ws.send_json({"error": "Invalid password"})
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WebSocket error: {ws.exception()}")
    except ConnectionResetError:
        pass
    except Exception as e:
        logger.error(f"Auth error: {e}")

    return ws


async def ws_handler(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """Handle WebSocket connections with PTY bridging."""
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    client_ip = request.remote if request.remote else "unknown"
    logger.info(f"WebSocket connection from {client_ip}")

    token = request.query.get("auth_token", "")

    if not token:
        logger.warning(f"Unauthorized connection from {client_ip}: missing auth_token")
        await ws.close(code=1008, message=b"Missing auth_token")
        return ws

    authenticator = Authenticator()
    if not authenticator.check_auth(token):
        logger.warning(f"Unauthorized connection from {client_ip}: invalid token")
        await ws.close(code=1008, message=b"Invalid token")
        return ws

    logger.info(f"Authenticated WebSocket connection from {client_ip}")

    command = request.app["command"]
    pty_manager = PtyManager(command=command)
    try:
        pty_manager.spawn()
    except Exception as e:
        logger.error(f"Failed to spawn PTY: {e}")
        await ws.close(code=1011, message=str(e).encode())
        return ws

    async def bridge_pty_to_ws() -> None:
        try:
            async for data in pty_manager.read_from_pty_async():
                await ws.send_bytes(data)
        except Exception as e:
            logger.debug(f"PTY to WS bridge error: {e}")

    async def bridge_ws_to_pty() -> None:
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "resize":
                            cols = data.get("cols", 80)
                            rows = data.get("rows", 24)
                            logger.info(f"Resizing PTY to {cols}x{rows}")
                            pty_manager.resize(cols, rows)
                            continue
                    except json.JSONDecodeError:
                        pass

                if msg.type == aiohttp.WSMsgType.BINARY:
                    if DEBUG_MESSAGES:
                        logger.debug(f"WS -> PTY: {len(msg.data)} bytes")
                    await pty_manager.write_to_pty(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    if DEBUG_MESSAGES:
                        logger.debug(f"WS -> PTY: {len(msg.data)} bytes")
                    data = msg.data.encode("utf-8") if isinstance(msg.data, str) else msg.data
                    await pty_manager.write_to_pty(data)
        except ConnectionResetError:
            logger.info("WebSocket closed by client")
        except Exception as e:
            logger.debug(f"WS to PTY bridge error: {e}")

    try:
        await asyncio.gather(
            bridge_pty_to_ws(),
            bridge_ws_to_pty(),
        )
    finally:
        logger.debug("Closing PTY manager")
        pty_manager.close()
        await ws.send_json({"type": "process_exit"})
        logger.info(f"Connection closed for {client_ip}")

    return ws


def create_app(command: str = DEFAULT_COMMAND) -> aiohttp.web.Application:
    """Create the aiohttp application."""
    app = aiohttp.web.Application()
    app["command"] = command
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/auth", auth_handler)
    return app


async def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    command: str = DEFAULT_COMMAND,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run the combined HTTP and WebSocket server.

    Args:
        host: Host to bind to (default 127.0.0.1).
        port: Port to bind to (default 4200).
        command: The command to run in the PTY (default: "hermes").
        shutdown_event: Optional event to signal shutdown initiation.

    """
    app = create_app(command=command)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()

    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"Server listening on {host}:{port}")

    if shutdown_event is None:
        await asyncio.Future()
    else:
        await shutdown_event.wait()
        logger.info("Shutdown signal received")

    await runner.cleanup()
