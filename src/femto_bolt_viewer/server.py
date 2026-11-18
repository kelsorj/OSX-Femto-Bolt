"""Loopback-only browser surface: independent color and native-depth images."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

WEB = Path(__file__).parent / "web"


def same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # Local non-browser clients do not send Origin.
    try:
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and parsed.netloc == websocket.headers.get("host")
    except ValueError:
        return False


def create_app(viewer) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        try:
            await asyncio.to_thread(viewer.start)
            yield
        finally:
            await asyncio.to_thread(viewer.close)

    app = FastAPI(title="OSX Femto Bolt", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", media_type="text/html")

    @app.get("/viewer.js")
    def javascript():
        return FileResponse(WEB / "viewer.js", media_type="text/javascript")

    @app.get("/viewer.css")
    def stylesheet():
        return FileResponse(WEB / "viewer.css", media_type="text/css")

    @app.get("/status")
    def status():
        return viewer.snapshot(include_images=False)

    @app.websocket("/frames")
    async def frames(websocket: WebSocket, max_mm: int = 800):
        if not same_origin(websocket) or max_mm not in (800, 2000, 6000):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                data = await asyncio.to_thread(viewer.snapshot, max_mm=max_mm)
                await websocket.send_json(data)
                await asyncio.sleep(1 / 15)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return

    return app
