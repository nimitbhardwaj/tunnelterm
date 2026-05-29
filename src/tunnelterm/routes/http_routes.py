"""Plain HTTP routes: index page and health check."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

STATIC_DIR = Path(__file__).parent.parent / "static"

_INDEX_HTML: bytes | None = None


def _read_index_html() -> bytes:
    """Return cached ``index.html`` bytes."""
    global _INDEX_HTML
    if _INDEX_HTML is None:
        _INDEX_HTML = (STATIC_DIR / "index.html").read_bytes()
    return _INDEX_HTML


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the SPA shell."""
    return HTMLResponse(content=_read_index_html())


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Trivial liveness probe."""
    return JSONResponse({"status": "ok"})
