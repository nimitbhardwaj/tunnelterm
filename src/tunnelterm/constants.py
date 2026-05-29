"""Process-wide constants shared between modules.

Kept in a tiny module so importing them never drags in FastAPI / asyncio /
the PTY layer; safe to import from anywhere including tests.
"""

from __future__ import annotations

#: Discriminator key for JSON control frames on the WebSocket. The presence
#: of this key is what distinguishes a control message from raw shell input
#: (whose bytes can legitimately contain JSON-looking text).
CONTROL_KEY = "__tt"

#: Largest text frame accepted from the client over /ws. A malicious or buggy
#: client could otherwise hold a worker hostage with a 100 MB payload. 1 MiB
#: is plenty for paste buffers; binary frames are not size-capped here because
#: they are streamed directly into the PTY write call.
MAX_WS_TEXT_FRAME_BYTES = 1 * 1024 * 1024
