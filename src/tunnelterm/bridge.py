"""WebSocket <-> PTY byte pump.

This is the only module that talks to a live :class:`~fastapi.WebSocket`
while a session is attached. It is intentionally narrow: everything else
(routing, auth, lifecycle) is decided before :func:`bridge_session` is
called.

The bridge:

* Replays any retained scrollback to the new client before going live.
* Subscribes a callback to PTY output and forwards it to the WebSocket.
* Reads client text frames and either dispatches them as control frames
  (when the discriminator key is present) or writes them to the PTY.
* Detaches cleanly when either side disconnects; the session itself keeps
  running so a subsequent reconnect with the same token re-attaches.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from tunnelterm.constants import CONTROL_KEY, MAX_WS_TEXT_FRAME_BYTES

if TYPE_CHECKING:
    from fastapi import WebSocket

    from tunnelterm.session import PtySession, SessionRegistry

logger = logging.getLogger(__name__)


async def bridge_session(
    ws: WebSocket,
    session: PtySession,
    registry: SessionRegistry | None,
    client_ip: str,
    replay: bool,
) -> None:
    """Pump bytes between ``ws`` and ``session`` until either side disconnects.

    Args:
        ws: The accepted WebSocket carrying the client.
        session: The sticky PTY session to bridge to.
        registry: The session registry, used to evict the session if its PTY
            died during this bridge. Pass ``None`` to skip eviction.
        client_ip: Source IP, used for log lines only.
        replay: If True, send the session's scrollback buffer before going live.

    """
    stop_event = asyncio.Event()
    # Bounded queue so a slow client back-pressures. We drop the oldest chunk
    # if the queue fills; a vanished client gets reaped on idle, not here.
    out_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1024)

    def _on_data(chunk: bytes) -> None:
        """Session-data callback (runs on the loop thread)."""
        if not chunk:
            try:
                out_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            out_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                out_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    if replay:
        snapshot = session.replay_buffer()
        if snapshot:
            try:
                await ws.send_bytes(snapshot)
            except Exception as e:
                logger.debug("scrollback replay send failed: %s", e)

    session.attach(_on_data)

    # Re-apply last known dimensions so the shell isn't confused after reattach.
    cols, rows = session.dimensions
    session.resize(cols=cols, rows=rows)

    async def pty_to_ws() -> None:
        while True:
            try:
                data = await out_queue.get()
            except asyncio.CancelledError:
                break
            if data is None:
                break
            try:
                await ws.send_bytes(data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("ws.send_bytes failed: %s", e)
                break
        stop_event.set()

    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                if len(msg) > MAX_WS_TEXT_FRAME_BYTES:
                    logger.warning(
                        "Client text frame %d B exceeds cap %d; disconnecting (%s)",
                        len(msg),
                        MAX_WS_TEXT_FRAME_BYTES,
                        client_ip,
                    )
                    break
                # Treat as a control frame only if it parses as a dict AND has
                # our discriminator key. Anything else is opaque shell input.
                if msg.startswith("{") and CONTROL_KEY in msg:
                    try:
                        data = json.loads(msg)
                    except (json.JSONDecodeError, ValueError):
                        data = None
                    if isinstance(data, dict) and CONTROL_KEY in data:
                        await _handle_control(data, session)
                        continue
                await session.write(msg.encode())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("WS->PTY ended: %s", e)
        finally:
            stop_event.set()

    pty_to_ws_task = asyncio.create_task(pty_to_ws())
    ws_to_pty_task = asyncio.create_task(ws_to_pty())

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.debug("ws bridge cancelled")
    finally:
        session.detach(_on_data)

        pty_died = not session.is_alive()
        if pty_died and registry is not None:
            registry.discard(session.token)

        for task in (pty_to_ws_task, ws_to_pty_task):
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(pty_to_ws_task, ws_to_pty_task, return_exceptions=True),
                timeout=1.0,
            )
        except TimeoutError:
            logger.debug("Bridge tasks did not exit within 1s")
        except asyncio.CancelledError:
            pass

        if pty_died:
            # Notify the client and close cleanly. Without an explicit
            # ws.close() FastAPI tears down the TCP socket with no WebSocket
            # close frame, which browsers see as code 1006.
            try:
                await ws.send_json({CONTROL_KEY: "process_exit"})
            except Exception:
                pass
            try:
                if ws.client_state.name != "DISCONNECTED":
                    await ws.close(code=1000)
            except Exception as e:
                logger.debug("ws.close() error: %s", e)
            logger.info(
                "Session ended for %s (PTY exited)",
                client_ip,
            )
        else:
            logger.info(
                "Connection detached for %s (session kept alive)",
                client_ip,
            )


async def _handle_control(msg: dict, session: PtySession) -> None:
    """Process a client -> server control frame."""
    kind = msg.get(CONTROL_KEY)
    if kind == "resize":
        try:
            cols = int(msg.get("cols", 0))
            rows = int(msg.get("rows", 0))
        except (TypeError, ValueError):
            return
        session.resize(cols=cols, rows=rows)
    elif kind == "ping":
        pass
